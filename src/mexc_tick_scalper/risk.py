from __future__ import annotations

from dataclasses import dataclass
import math


@dataclass(slots=True)
class PositionPlan:
    bankroll_usdt: float
    margin_usdt: float
    leverage: int
    target_notional_usdt: float
    qty: float
    confidence: float


def confidence_from_profit_factor(pf: float) -> float:
    """Conservative size multiplier derived from current validated PF."""
    if pf < 1.30:
        return 0.0
    if pf < 1.50:
        return 0.25
    if pf < 1.80:
        return 0.50
    if pf < 2.20:
        return 0.75
    return 1.0


def volatility_leverage_cap(
    stress_move_bps: float,
    hard_cap: int,
    liquidation_buffer_fraction: float = 0.35,
) -> int:
    """Estimate a leverage cap from an adverse stress move.

    This is deliberately conservative and is not an exchange liquidation-price
    calculator. It reserves only a fraction of the theoretical inverse move as
    usable leverage so normal slippage/gaps do not sit near liquidation.
    """
    if stress_move_bps <= 0:
        return max(1, hard_cap)
    stress_fraction = stress_move_bps / 10_000.0
    raw = liquidation_buffer_fraction / stress_fraction
    return max(1, min(hard_cap, int(math.floor(raw))))


def make_position_plan(
    *,
    price: float,
    bankroll_usdt: float,
    base_margin_fraction: float,
    max_margin_per_trade_usdt: float,
    hard_max_leverage: int,
    exchange_max_leverage: int,
    stress_move_bps: float,
    validated_profit_factor: float,
) -> PositionPlan | None:
    if price <= 0 or bankroll_usdt <= 0:
        return None

    confidence = confidence_from_profit_factor(validated_profit_factor)
    if confidence <= 0:
        return None

    base_margin = bankroll_usdt * base_margin_fraction
    margin = min(max_margin_per_trade_usdt, base_margin) * confidence
    if margin <= 0:
        return None

    vol_cap = volatility_leverage_cap(stress_move_bps, hard_max_leverage)
    leverage = max(1, min(hard_max_leverage, exchange_max_leverage, vol_cap))
    notional = margin * leverage
    qty = notional / price

    return PositionPlan(
        bankroll_usdt=bankroll_usdt,
        margin_usdt=margin,
        leverage=leverage,
        target_notional_usdt=notional,
        qty=qty,
        confidence=confidence,
    )
