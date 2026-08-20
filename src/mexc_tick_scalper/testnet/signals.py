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


def directional_move_bps(direction: int, start: float, end: float) -> float:
    if start <= 0 or end <= 0 or direction not in (-1, 1):
        return 0.0
    return direction * (end / start - 1.0) * 10_000.0


def impulse_retention_fraction(
    direction: int,
    signal_binance_price: float,
    signal_binance_move_bps: float,
    current_binance_price: float,
) -> float:
    """Preserve the original persistent-catchup impulse-retention calculation."""
    move = direction * signal_binance_move_bps
    if move <= 0 or signal_binance_price <= 0 or current_binance_price <= 0:
        return 0.0
    pre_price = signal_binance_price / (1.0 + direction * move / 10_000.0)
    original = directional_move_bps(direction, pre_price, signal_binance_price)
    current = directional_move_bps(direction, pre_price, current_binance_price)
    if original <= 0:
        return 0.0
    return current / original


def arrival_entry_ok(
    *,
    signal: TradeSignal,
    current_residual_bps: float,
    current_binance_price: float,
    current_spread_bps: float,
    min_remaining_edge_bps: float,
    min_edge_after_spread_bps: float,
) -> tuple[bool, str, float, float]:
    """Preserve current baseline-v1 arrival economics without legacy globals.

    The current validated auto-discovery implementation computes retention metrics
    for diagnostics but gates on direction and absolute executable edge. Do not
    silently tighten this function to the older retention-gated variant.
    """
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
