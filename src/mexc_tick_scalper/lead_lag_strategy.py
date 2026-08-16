from __future__ import annotations

import math
import statistics
from collections import deque
from dataclasses import dataclass, field

from .microspread import MicroSpreadSnapshot


@dataclass(frozen=True, slots=True)
class LagDecision:
    ready: bool
    reason: str
    direction: int
    residual_bps: float
    threshold_bps: float
    noise_bps: float
    binance_move_bps: float
    mexc_move_bps: float
    leader_advantage_bps: float


@dataclass(slots=True)
class _SymbolState:
    residuals: deque[tuple[int, float]] = field(default_factory=lambda: deque(maxlen=4096))
    candidate_direction: int = 0
    candidate_count: int = 0
    candidate_since_ms: int = 0
    armed: bool = True
    last_signal_direction: int = 0
    last_event_key: tuple[int, int] | None = None


class LeadLagGate:
    """Adaptive noise filter for Binance-leading / MEXC-lagging entries.

    A trade is allowed only when the residual is large versus both the live MEXC
    spread and recent residual noise, Binance moved materially in the residual
    direction, Binance moved more than MEXC, and the condition survived more
    than one independent market update.
    """

    def __init__(
        self,
        *,
        noise_window_ms: int = 8_000,
        residual_noise_multiplier: float = 3.0,
        binance_noise_multiplier: float = 1.0,
        min_edge_bps: float = 0.50,
        min_net_edge_bps: float = 0.20,
        spread_ratio: float = 1.05,
        min_binance_move_bps: float = 0.50,
        min_leader_advantage_bps: float = 0.25,
        min_lead_ratio: float = 1.25,
        confirm_updates: int = 2,
        confirm_ms: int = 15,
        rearm_fraction: float = 0.35,
    ) -> None:
        self.noise_window_ms = max(2_000, int(noise_window_ms))
        self.residual_noise_multiplier = max(0.0, float(residual_noise_multiplier))
        self.binance_noise_multiplier = max(0.0, float(binance_noise_multiplier))
        self.min_edge_bps = max(0.0, float(min_edge_bps))
        self.min_net_edge_bps = max(0.0, float(min_net_edge_bps))
        self.spread_ratio = max(1.0, float(spread_ratio))
        self.min_binance_move_bps = max(0.0, float(min_binance_move_bps))
        self.min_leader_advantage_bps = max(0.0, float(min_leader_advantage_bps))
        self.min_lead_ratio = max(1.0, float(min_lead_ratio))
        self.confirm_updates = max(1, int(confirm_updates))
        self.confirm_ms = max(0, int(confirm_ms))
        self.rearm_fraction = min(0.8, max(0.05, float(rearm_fraction)))
        self._state: dict[str, _SymbolState] = {}

    def _s(self, symbol: str) -> _SymbolState:
        return self._state.setdefault(symbol, _SymbolState())

    @staticmethod
    def _mad_noise(values: list[float]) -> float:
        if len(values) < 12:
            return 0.0
        med = statistics.median(values)
        mad = statistics.median(abs(x - med) for x in values)
        return 1.4826 * mad

    def _noise(self, state: _SymbolState, now_ms: int) -> float:
        cutoff = now_ms - self.noise_window_ms
        while state.residuals and state.residuals[0][0] < cutoff:
            state.residuals.popleft()
        return self._mad_noise([x for _, x in state.residuals])

    def assess(self, symbol: str, snap: MicroSpreadSnapshot, spread_bps: float, now_ms: int) -> LagDecision:
        state = self._s(symbol)
        noise = self._noise(state, now_ms)
        threshold = max(
            self.min_edge_bps,
            float(spread_bps) + self.min_net_edge_bps,
            float(spread_bps) * self.spread_ratio,
            noise * self.residual_noise_multiplier,
        )
        direction = 1 if snap.edge_bps > 0 else -1 if snap.edge_bps < 0 else 0
        b_dir = direction * float(snap.binance_move_bps)
        m_dir = direction * float(snap.mexc_move_bps)
        leader_advantage = b_dir - m_dir
        b_required = max(self.min_binance_move_bps, noise * self.binance_noise_multiplier)

        if not math.isfinite(snap.binance_age_ms) or not math.isfinite(snap.mexc_age_ms):
            reason = "warming"
        elif direction == 0:
            reason = "no_direction"
        elif abs(float(snap.edge_bps)) < threshold:
            reason = "residual_below_adaptive_threshold"
        elif b_dir < b_required:
            reason = "binance_move_is_noise"
        elif leader_advantage < self.min_leader_advantage_bps:
            reason = "mexc_not_lagging_enough"
        elif m_dir > 0 and b_dir < abs(m_dir) * self.min_lead_ratio:
            reason = "binance_not_leading_enough"
        else:
            reason = "lag_confirmable"

        return LagDecision(
            ready=reason == "lag_confirmable",
            reason=reason,
            direction=direction,
            residual_bps=float(snap.edge_bps),
            threshold_bps=float(threshold),
            noise_bps=float(noise),
            binance_move_bps=float(snap.binance_move_bps),
            mexc_move_bps=float(snap.mexc_move_bps),
            leader_advantage_bps=float(leader_advantage),
        )

    def observe(
        self,
        symbol: str,
        snap: MicroSpreadSnapshot,
        spread_bps: float,
        now_ms: int,
        *,
        event_key: tuple[int, int] | None = None,
    ) -> LagDecision:
        state = self._s(symbol)
        if event_key is not None and state.last_event_key == event_key:
            decision = self.assess(symbol, snap, spread_bps, now_ms)
            return LagDecision(False, "duplicate_market_state", decision.direction, decision.residual_bps,
                               decision.threshold_bps, decision.noise_bps, decision.binance_move_bps,
                               decision.mexc_move_bps, decision.leader_advantage_bps)
        state.last_event_key = event_key
        state.residuals.append((now_ms, float(snap.edge_bps)))
        decision = self.assess(symbol, snap, spread_bps, now_ms)

        rearm_level = max(0.05, decision.threshold_bps * self.rearm_fraction)
        if not state.armed and (
            abs(decision.residual_bps) <= rearm_level
            or (decision.direction and decision.direction != state.last_signal_direction)
        ):
            state.armed = True
            state.candidate_direction = 0
            state.candidate_count = 0
            state.candidate_since_ms = 0

        if not decision.ready:
            state.candidate_direction = 0
            state.candidate_count = 0
            state.candidate_since_ms = 0
            return decision
        if not state.armed:
            return LagDecision(False, "lag_not_rearmed", decision.direction, decision.residual_bps,
                               decision.threshold_bps, decision.noise_bps, decision.binance_move_bps,
                               decision.mexc_move_bps, decision.leader_advantage_bps)

        if state.candidate_direction != decision.direction:
            state.candidate_direction = decision.direction
            state.candidate_count = 1
            state.candidate_since_ms = now_ms
        else:
            state.candidate_count += 1

        if state.candidate_count < self.confirm_updates or now_ms - state.candidate_since_ms < self.confirm_ms:
            return LagDecision(False, "lag_confirming", decision.direction, decision.residual_bps,
                               decision.threshold_bps, decision.noise_bps, decision.binance_move_bps,
                               decision.mexc_move_bps, decision.leader_advantage_bps)

        state.armed = False
        state.last_signal_direction = decision.direction
        state.candidate_direction = 0
        state.candidate_count = 0
        state.candidate_since_ms = 0
        return decision


def spread_aware_adverse_cut(entry_spread_bps: float, base_cut_bps: float = 1.5, spread_multiple: float = 1.25) -> float:
    return max(float(base_cut_bps), max(0.0, float(entry_spread_bps)) * max(1.0, float(spread_multiple)))


def convergence_threshold(entry_residual_bps: float, floor_bps: float = 0.10, fraction: float = 0.20) -> float:
    return max(max(0.0, float(floor_bps)), abs(float(entry_residual_bps)) * max(0.0, float(fraction)))
