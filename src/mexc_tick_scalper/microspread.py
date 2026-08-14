from __future__ import annotations

import math
import statistics
import time
from collections import deque
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class MicroSpreadSnapshot:
    ready: bool
    direction: int
    edge_bps: float
    raw_gap_bps: float
    baseline_gap_bps: float
    binance_move_bps: float
    mexc_move_bps: float
    binance_mid: float
    mexc_mid: float
    age_ms: float
    threshold_bps: float
    reason: str


class MicroSpreadModel:
    """Detect short Binance/MEXC basis excursions instead of rare large impulses.

    The model continuously tracks log(Binance mid / MEXC mid).  A robust rolling
    median is the normal cross-exchange basis.  The tradable edge is the current
    gap minus that baseline.  A signal is emitted once when the residual crosses
    an executable threshold, then it must converge before the model rearms.

    This is intentionally different from the older LeadLagModel: a 1 bps
    Binance impulse is not required.  Tiny leader moves are enough when they
    create a fresh residual that is large relative to the live MEXC spread.
    """

    def __init__(
        self,
        *,
        horizon_ms: int = 100,
        baseline_seconds: float = 8.0,
        min_edge_bps: float = 0.35,
        min_binance_move_bps: float = 0.05,
        max_age_ms: float = 250.0,
        rearm_fraction: float = 0.35,
        min_baseline_points: int = 12,
    ) -> None:
        self.horizon_ms = max(20, int(horizon_ms))
        self.baseline_ms = max(1_000, int(float(baseline_seconds) * 1000))
        self.min_edge_bps = max(0.0, float(min_edge_bps))
        self.min_binance_move_bps = max(0.0, float(min_binance_move_bps))
        self.max_age_ms = max(20.0, float(max_age_ms))
        self.rearm_fraction = min(0.8, max(0.05, float(rearm_fraction)))
        self.min_baseline_points = max(3, int(min_baseline_points))

        self.binance: deque[tuple[int, float]] = deque(maxlen=50_000)
        self.mexc: deque[tuple[int, float]] = deque(maxlen=50_000)
        self.gaps: deque[tuple[int, float]] = deque(maxlen=50_000)
        self._armed = True
        self._last_signal_direction = 0

    @staticmethod
    def _mid(bid: float, ask: float) -> float:
        return (bid + ask) / 2.0 if ask > bid > 0 else 0.0

    @staticmethod
    def _trim(rows: deque[tuple[int, float]], cutoff_ms: int) -> None:
        while rows and rows[0][0] < cutoff_ms:
            rows.popleft()

    @staticmethod
    def _past_price(rows: deque[tuple[int, float]], target_ms: int) -> float | None:
        if not rows:
            return None
        chosen = rows[0][1]
        for ts_ms, price in rows:
            if ts_ms > target_ms:
                break
            chosen = price
        return chosen

    def _append(self, rows: deque[tuple[int, float]], ts_ms: int, price: float) -> None:
        rows.append((int(ts_ms), float(price)))
        cutoff = int(ts_ms) - max(self.baseline_ms, self.horizon_ms) * 2
        self._trim(rows, cutoff)
        self._update_gap(int(ts_ms))

    def update_binance(self, *, bid: float, ask: float, ts_ms: int | None = None) -> None:
        mid = self._mid(float(bid), float(ask))
        if mid <= 0:
            return
        ts = int(ts_ms if ts_ms is not None else time.time() * 1000)
        self._append(self.binance, ts, mid)

    def update_mexc(self, *, bid: float, ask: float, ts_ms: int | None = None) -> None:
        mid = self._mid(float(bid), float(ask))
        if mid <= 0:
            return
        ts = int(ts_ms if ts_ms is not None else time.time() * 1000)
        self._append(self.mexc, ts, mid)

    def update_mexc_price(self, *, price: float, ts_ms: int | None = None) -> None:
        if price <= 0:
            return
        ts = int(ts_ms if ts_ms is not None else time.time() * 1000)
        self._append(self.mexc, ts, float(price))

    def _update_gap(self, ts_ms: int) -> None:
        if not self.binance or not self.mexc:
            return
        b_mid = self.binance[-1][1]
        m_mid = self.mexc[-1][1]
        if b_mid <= 0 or m_mid <= 0:
            return
        self.gaps.append((ts_ms, math.log(b_mid / m_mid) * 10_000.0))
        self._trim(self.gaps, ts_ms - self.baseline_ms)

    def snapshot(self, *, now_ms: int | None = None, threshold_bps: float | None = None) -> MicroSpreadSnapshot:
        now = int(now_ms if now_ms is not None else time.time() * 1000)
        threshold = max(self.min_edge_bps, float(threshold_bps or 0.0))
        if not self.binance or not self.mexc or len(self.gaps) < self.min_baseline_points:
            return MicroSpreadSnapshot(False, 0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, math.inf, threshold, "warming_up")

        b_ts, b_mid = self.binance[-1]
        m_ts, m_mid = self.mexc[-1]
        age_ms = float(max(now - b_ts, now - m_ts))
        if age_ms < 0 or age_ms > self.max_age_ms:
            return MicroSpreadSnapshot(False, 0, 0.0, 0.0, 0.0, 0.0, 0.0, b_mid, m_mid, age_ms, threshold, "stale_quotes")

        raw_gap = math.log(b_mid / m_mid) * 10_000.0
        # Exclude the newest horizon from the baseline so the excursion cannot
        # instantly teach itself away. Median is robust to transient spikes.
        baseline_values = [gap for ts, gap in self.gaps if ts <= now - self.horizon_ms]
        if len(baseline_values) < self.min_baseline_points:
            return MicroSpreadSnapshot(False, 0, 0.0, raw_gap, 0.0, 0.0, 0.0, b_mid, m_mid, age_ms, threshold, "warming_baseline")
        baseline = float(statistics.median(baseline_values))
        edge = raw_gap - baseline
        direction = 1 if edge > 0 else -1 if edge < 0 else 0

        target = now - self.horizon_ms
        b_old = self._past_price(self.binance, target)
        m_old = self._past_price(self.mexc, target)
        if not b_old or not m_old:
            return MicroSpreadSnapshot(False, direction, edge, raw_gap, baseline, 0.0, 0.0, b_mid, m_mid, age_ms, threshold, "warming_horizon")

        b_move = math.log(b_mid / b_old) * 10_000.0
        m_move = math.log(m_mid / m_old) * 10_000.0

        if abs(edge) < threshold:
            reason = "microspread_below_threshold"
            ready = False
        elif direction == 0:
            reason = "no_direction"
            ready = False
        elif abs(b_move) < self.min_binance_move_bps:
            reason = "leader_micro_move_too_small"
            ready = False
        elif direction * b_move <= 0:
            reason = "leader_direction_mismatch"
            ready = False
        else:
            reason = "microspread_confirmed"
            ready = True

        return MicroSpreadSnapshot(
            ready=ready,
            direction=direction,
            edge_bps=edge,
            raw_gap_bps=raw_gap,
            baseline_gap_bps=baseline,
            binance_move_bps=b_move,
            mexc_move_bps=m_move,
            binance_mid=b_mid,
            mexc_mid=m_mid,
            age_ms=age_ms,
            threshold_bps=threshold,
            reason=reason,
        )

    def signal(self, *, now_ms: int | None = None, threshold_bps: float | None = None) -> MicroSpreadSnapshot:
        snap = self.snapshot(now_ms=now_ms, threshold_bps=threshold_bps)
        threshold = snap.threshold_bps
        rearm_level = max(0.05, threshold * self.rearm_fraction)

        # Rearm only after convergence toward the normal basis. A direct sign
        # flip also implies a zero crossing between observations and may rearm.
        if not self._armed:
            if abs(snap.edge_bps) <= rearm_level or (
                snap.direction != 0 and snap.direction != self._last_signal_direction
            ):
                self._armed = True

        if not snap.ready:
            return snap
        if not self._armed:
            return MicroSpreadSnapshot(
                False,
                snap.direction,
                snap.edge_bps,
                snap.raw_gap_bps,
                snap.baseline_gap_bps,
                snap.binance_move_bps,
                snap.mexc_move_bps,
                snap.binance_mid,
                snap.mexc_mid,
                snap.age_ms,
                snap.threshold_bps,
                "microspread_not_rearmed",
            )

        self._armed = False
        self._last_signal_direction = snap.direction
        return snap
