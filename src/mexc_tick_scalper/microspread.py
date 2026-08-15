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
    binance_age_ms: float
    mexc_age_ms: float
    threshold_bps: float
    reason: str


class MicroSpreadModel:
    """Detect short Binance/MEXC basis excursions instead of rare large impulses.

    The normal cross-exchange basis is a rolling median.  Entry is based on the
    residual from that basis and does not require a 1 bps leader impulse.  The
    newest part of the gap series is excluded from the baseline so a short lag
    cannot immediately teach itself away.
    """

    def __init__(
        self,
        *,
        horizon_ms: int = 100,
        baseline_seconds: float = 8.0,
        baseline_exclusion_ms: int = 1000,
        min_edge_bps: float = 0.35,
        min_binance_move_bps: float = 0.05,
        max_binance_age_ms: float = 300.0,
        max_mexc_age_ms: float = 2000.0,
        rearm_fraction: float = 0.35,
        min_baseline_points: int = 12,
    ) -> None:
        self.horizon_ms = max(20, int(horizon_ms))
        self.baseline_ms = max(2_000, int(float(baseline_seconds) * 1000))
        self.baseline_exclusion_ms = max(self.horizon_ms, int(baseline_exclusion_ms))
        self.min_edge_bps = max(0.0, float(min_edge_bps))
        self.min_binance_move_bps = max(0.0, float(min_binance_move_bps))
        self.max_binance_age_ms = max(20.0, float(max_binance_age_ms))
        self.max_mexc_age_ms = max(50.0, float(max_mexc_age_ms))
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
        cutoff = int(ts_ms) - max(self.baseline_ms, self.baseline_exclusion_ms) * 2
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

    def _empty(self, threshold: float, reason: str) -> MicroSpreadSnapshot:
        return MicroSpreadSnapshot(
            False, 0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0,
            math.inf, math.inf, math.inf, threshold, reason,
        )

    def snapshot(self, *, now_ms: int | None = None, threshold_bps: float | None = None) -> MicroSpreadSnapshot:
        now = int(now_ms if now_ms is not None else time.time() * 1000)
        threshold = max(self.min_edge_bps, float(threshold_bps or 0.0))
        if not self.binance or not self.mexc or len(self.gaps) < self.min_baseline_points:
            return self._empty(threshold, "warming_up")

        b_ts, b_mid = self.binance[-1]
        m_ts, m_mid = self.mexc[-1]
        b_age = float(now - b_ts)
        m_age = float(now - m_ts)
        age_ms = max(b_age, m_age)
        if b_age < 0 or b_age > self.max_binance_age_ms:
            return MicroSpreadSnapshot(False, 0, 0.0, 0.0, 0.0, 0.0, 0.0, b_mid, m_mid, age_ms, b_age, m_age, threshold, "stale_binance")
        if m_age < 0 or m_age > self.max_mexc_age_ms:
            return MicroSpreadSnapshot(False, 0, 0.0, 0.0, 0.0, 0.0, 0.0, b_mid, m_mid, age_ms, b_age, m_age, threshold, "stale_mexc")

        raw_gap = math.log(b_mid / m_mid) * 10_000.0
        baseline_values = [
            gap for ts, gap in self.gaps
            if ts <= now - self.baseline_exclusion_ms
        ]
        if len(baseline_values) < self.min_baseline_points:
            return MicroSpreadSnapshot(False, 0, 0.0, raw_gap, 0.0, 0.0, 0.0, b_mid, m_mid, age_ms, b_age, m_age, threshold, "warming_baseline")
        baseline = float(statistics.median(baseline_values))
        edge = raw_gap - baseline
        direction = 1 if edge > 0 else -1 if edge < 0 else 0

        target = now - self.horizon_ms
        b_old = self._past_price(self.binance, target)
        m_old = self._past_price(self.mexc, target)
        if not b_old or not m_old:
            return MicroSpreadSnapshot(False, direction, edge, raw_gap, baseline, 0.0, 0.0, b_mid, m_mid, age_ms, b_age, m_age, threshold, "warming_horizon")

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
            binance_age_ms=b_age,
            mexc_age_ms=m_age,
            threshold_bps=threshold,
            reason=reason,
        )

    def signal(self, *, now_ms: int | None = None, threshold_bps: float | None = None) -> MicroSpreadSnapshot:
        snap = self.snapshot(now_ms=now_ms, threshold_bps=threshold_bps)
        rearm_level = max(0.05, snap.threshold_bps * self.rearm_fraction)

        if not self._armed:
            if abs(snap.edge_bps) <= rearm_level or (
                snap.direction != 0 and snap.direction != self._last_signal_direction
            ):
                self._armed = True

        if not snap.ready:
            return snap
        if not self._armed:
            return MicroSpreadSnapshot(
                False, snap.direction, snap.edge_bps, snap.raw_gap_bps,
                snap.baseline_gap_bps, snap.binance_move_bps, snap.mexc_move_bps,
                snap.binance_mid, snap.mexc_mid, snap.age_ms, snap.binance_age_ms,
                snap.mexc_age_ms, snap.threshold_bps, "microspread_not_rearmed",
            )

        self._armed = False
        self._last_signal_direction = snap.direction
        return snap


class BinanceImpulseModel:
    """Emit hysteretic entry signals from Binance bookTicker movement alone.

    MEXC/Demo quotes may still be cached for execution and telemetry, but they
    never participate in the entry direction or readiness decision.
    """

    def __init__(
        self,
        *,
        horizon_ms: int = 100,
        min_edge_bps: float = 1.0,
        max_binance_age_ms: float = 300.0,
        rearm_fraction: float = 0.35,
        **_: object,
    ) -> None:
        self.horizon_ms = max(20, int(horizon_ms))
        self.min_edge_bps = max(0.0, float(min_edge_bps))
        self.max_binance_age_ms = max(20.0, float(max_binance_age_ms))
        self.rearm_fraction = min(0.8, max(0.05, float(rearm_fraction)))
        self.binance: deque[tuple[int, float]] = deque(maxlen=20_000)
        self.mexc: deque[tuple[int, float]] = deque(maxlen=2)
        self._armed = True
        self._last_signal_direction = 0

    def update_binance(self, *, bid: float, ask: float, ts_ms: int | None = None) -> None:
        mid = MicroSpreadModel._mid(float(bid), float(ask))
        if mid <= 0:
            return
        ts = int(ts_ms if ts_ms is not None else time.time() * 1000)
        self.binance.append((ts, mid))
        MicroSpreadModel._trim(self.binance, ts - self.horizon_ms * 4)

    def update_mexc(self, *, bid: float, ask: float, ts_ms: int | None = None) -> None:
        mid = MicroSpreadModel._mid(float(bid), float(ask))
        if mid <= 0:
            return
        ts = int(ts_ms if ts_ms is not None else time.time() * 1000)
        self.mexc.append((ts, mid))

    def snapshot(self, *, now_ms: int | None = None, threshold_bps: float | None = None) -> MicroSpreadSnapshot:
        now = int(now_ms if now_ms is not None else time.time() * 1000)
        threshold = max(self.min_edge_bps, float(threshold_bps or 0.0))
        if not self.binance:
            return MicroSpreadSnapshot(
                False, 0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0,
                math.inf, math.inf, math.inf, threshold, "warming_up",
            )
        b_ts, b_mid = self.binance[-1]
        b_age = float(now - b_ts)
        mexc_mid = self.mexc[-1][1] if self.mexc else 0.0
        mexc_age = float(now - self.mexc[-1][0]) if self.mexc else math.inf
        if b_age < 0 or b_age > self.max_binance_age_ms:
            return MicroSpreadSnapshot(
                False, 0, 0.0, 0.0, 0.0, 0.0, 0.0, b_mid, mexc_mid,
                b_age, b_age, mexc_age, threshold, "stale_binance",
            )
        old = MicroSpreadModel._past_price(self.binance, now - self.horizon_ms)
        if not old or old == b_mid and len(self.binance) < 2:
            return MicroSpreadSnapshot(
                False, 0, 0.0, 0.0, 0.0, 0.0, 0.0, b_mid, mexc_mid,
                b_age, b_age, mexc_age, threshold, "warming_horizon",
            )
        move = math.log(b_mid / old) * 10_000.0
        direction = 1 if move > 0 else -1 if move < 0 else 0
        ready = direction != 0 and abs(move) >= threshold
        reason = "binance_impulse_confirmed" if ready else "binance_impulse_below_threshold"
        raw_gap = math.log(b_mid / mexc_mid) * 10_000.0 if mexc_mid > 0 else 0.0
        return MicroSpreadSnapshot(
            ready, direction, move, raw_gap, 0.0, move, 0.0, b_mid, mexc_mid,
            max(b_age, mexc_age), b_age, mexc_age, threshold, reason,
        )

    def signal(self, *, now_ms: int | None = None, threshold_bps: float | None = None) -> MicroSpreadSnapshot:
        snap = self.snapshot(now_ms=now_ms, threshold_bps=threshold_bps)
        rearm_level = max(0.05, snap.threshold_bps * self.rearm_fraction)
        if not self._armed and (
            abs(snap.binance_move_bps) <= rearm_level
            or (snap.direction and snap.direction != self._last_signal_direction)
        ):
            self._armed = True
        if not snap.ready or not self._armed:
            if snap.ready and not self._armed:
                return MicroSpreadSnapshot(
                    False, snap.direction, snap.edge_bps, snap.raw_gap_bps,
                    snap.baseline_gap_bps, snap.binance_move_bps, snap.mexc_move_bps,
                    snap.binance_mid, snap.mexc_mid, snap.age_ms, snap.binance_age_ms,
                    snap.mexc_age_ms, snap.threshold_bps, "binance_impulse_not_rearmed",
                )
            return snap
        self._armed = False
        self._last_signal_direction = snap.direction
        return snap
