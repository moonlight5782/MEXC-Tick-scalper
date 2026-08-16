from __future__ import annotations

import math

from .lead_lag_strategy import LagDecision, LeadLagGate


class PersistentDelayedEntryGate(LeadLagGate):
    """Use strict Binance-lead confirmation for signal creation, but residual economics for delayed entry.

    `observe()` is the primary signal path and remains identical to LeadLagGate. Direct
    `assess()` calls (used by the delayed-entry checkpoint in the V2 shadow) no longer
    require a second fresh Binance impulse. They only require that the original lag has
    not reversed and that the remaining residual still clears live spread economics.
    """

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._primary_context = False

    def observe(self, symbol, snap, spread_bps, now_ms, *, event_key=None):
        self._primary_context = True
        try:
            return super().observe(symbol, snap, spread_bps, now_ms, event_key=event_key)
        finally:
            self._primary_context = False

    def assess(self, symbol, snap, spread_bps, now_ms):
        if self._primary_context:
            return super().assess(symbol, snap, spread_bps, now_ms)

        state = self._s(symbol)
        noise = self._noise(state, now_ms)
        threshold = max(
            self.min_edge_bps,
            float(spread_bps) + self.min_net_edge_bps,
            float(spread_bps) * self.spread_ratio,
        )
        direction = 1 if snap.edge_bps > 0 else -1 if snap.edge_bps < 0 else 0
        b_dir = direction * float(snap.binance_move_bps)
        m_dir = direction * float(snap.mexc_move_bps)
        leader_advantage = b_dir - m_dir

        if not math.isfinite(snap.binance_age_ms) or not math.isfinite(snap.mexc_age_ms):
            reason = "warming"
        elif direction == 0:
            reason = "no_direction"
        elif abs(float(snap.edge_bps)) < threshold:
            reason = "remaining_residual_not_economic"
        else:
            reason = "persistent_residual_exploitable"

        return LagDecision(
            ready=reason == "persistent_residual_exploitable",
            reason=reason,
            direction=direction,
            residual_bps=float(snap.edge_bps),
            threshold_bps=float(threshold),
            noise_bps=float(noise),
            binance_move_bps=float(snap.binance_move_bps),
            mexc_move_bps=float(snap.mexc_move_bps),
            leader_advantage_bps=float(leader_advantage),
        )


def delayed_entry_is_exploitable(
    *,
    signal_direction: int,
    signal_residual_bps: float,
    current_residual_bps: float,
    current_spread_bps: float,
    min_net_edge_bps: float,
    min_retention_fraction: float,
    min_remaining_edge_bps: float,
) -> tuple[bool, str]:
    current_direction = 1 if current_residual_bps > 0 else -1 if current_residual_bps < 0 else 0
    if current_direction != signal_direction:
        return False, "residual_reversed"
    required = max(
        float(min_remaining_edge_bps),
        max(0.0, float(current_spread_bps)) + max(0.0, float(min_net_edge_bps)),
    )
    if abs(float(current_residual_bps)) < required:
        return False, "remaining_edge_too_small"
    initial = abs(float(signal_residual_bps))
    if initial > 0 and abs(float(current_residual_bps)) / initial < max(0.0, float(min_retention_fraction)):
        return False, "retention_too_low"
    return True, "exploitable"
