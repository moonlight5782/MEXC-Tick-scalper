from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class TradeSignal:
    signal_id: str
    ts_ms: int
    symbol: str
    direction: int
    residual_bps: float
    threshold_bps: float
    noise_bps: float
    spread_bps: float
    leader_advantage_bps: float
    binance_move_bps: float
    mexc_move_bps: float
    binance_price: float
    mexc_price: float


def impulse_retention_fraction(
    direction: int,
    signal_binance_price: float,
    signal_binance_move_bps: float,
    current_binance_price: float,
) -> float:
    """Fraction of the original directional Binance impulse still present."""
    if direction not in (-1, 1) or signal_binance_price <= 0 or current_binance_price <= 0:
        return 0.0
    original = abs(float(signal_binance_move_bps))
    if original <= 1e-12:
        return 0.0
    current_move = direction * (current_binance_price - signal_binance_price) / signal_binance_price * 10_000.0
    retained = original + current_move
    return max(0.0, retained / original)


def arrival_entry_ok(
    *,
    signal: TradeSignal,
    current_residual_bps: float,
    current_binance_price: float,
    current_spread_bps: float,
    min_remaining_edge_bps: float,
    min_edge_after_spread_bps: float,
) -> tuple[bool, str, float, float]:
    """Preserve baseline-v1 arrival economics without legacy global side effects."""
    if signal.direction * current_residual_bps <= 0:
        return False, "residual_reversed", 0.0, 0.0

    residual_retention = abs(current_residual_bps) / max(abs(signal.residual_bps), 1e-12)
    impulse_retention = impulse_retention_fraction(
        signal.direction,
        signal.binance_price,
        signal.binance_move_bps,
        current_binance_price,
    )
    required = max(
        float(min_remaining_edge_bps),
        max(0.0, float(current_spread_bps)) + max(0.0, float(min_edge_after_spread_bps)),
    )
    if abs(float(current_residual_bps)) < required:
        return False, "remaining_edge_too_small", residual_retention, impulse_retention
    return True, "absolute_edge_survived", residual_retention, impulse_retention
