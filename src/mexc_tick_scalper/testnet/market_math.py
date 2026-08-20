from __future__ import annotations

import math
from dataclasses import dataclass

from ..microspread_feed import LiveBook


@dataclass(frozen=True, slots=True)
class VirtualIocFill:
    qty: float
    avg_price: float
    requested_qty: float
    limit_price: float

    @property
    def fill_ratio(self) -> float:
        return min(1.0, self.qty / self.requested_qty) if self.requested_qty > 0 else 0.0


def signed_move_bps(direction: int, entry: float, current: float) -> float:
    if direction not in (-1, 1) or entry <= 0 or current <= 0:
        return 0.0
    return direction * (current - entry) / entry * 10_000.0


def directional_move_bps(direction: int, start: float, current: float) -> float:
    return signed_move_bps(direction, start, current)


def exit_depth_for_qty(
    book: LiveBook,
    *,
    direction: int,
    qty: float,
    contract_size: float,
) -> tuple[float, float]:
    if qty <= 0 or contract_size <= 0:
        return 0.0, 0.0
    levels = book.asks if direction < 0 else book.bids
    remaining_qty = qty
    filled = 0.0
    quote = 0.0
    for price, contracts in levels:
        if price <= 0 or contracts <= 0:
            continue
        available = float(contracts) * float(contract_size)
        take = min(remaining_qty, available)
        filled += take
        quote += take * float(price)
        remaining_qty -= take
        if remaining_qty <= 1e-12:
            break
    return filled, (quote / filled if filled > 0 else 0.0)


def virtual_ioc_fill(
    book: LiveBook,
    *,
    direction: int,
    target_notional_usdt: float,
    contract_size: float,
    cross_bps: float,
) -> VirtualIocFill:
    if direction not in (-1, 1) or target_notional_usdt <= 0 or contract_size <= 0:
        return VirtualIocFill(0.0, 0.0, 0.0, 0.0)

    best = book.ask if direction > 0 else book.bid
    if best <= 0:
        return VirtualIocFill(0.0, 0.0, 0.0, 0.0)

    requested_qty = target_notional_usdt / best
    if direction > 0:
        limit_price = best * (1.0 + max(0.0, cross_bps) / 10_000.0)
        levels = book.asks
        allowed = lambda price: price <= limit_price + 1e-15
    else:
        limit_price = best * (1.0 - max(0.0, cross_bps) / 10_000.0)
        levels = book.bids
        allowed = lambda price: price >= limit_price - 1e-15

    remaining = requested_qty
    filled = 0.0
    quote = 0.0
    for price, contracts in levels:
        if price <= 0 or contracts <= 0 or not allowed(price):
            continue
        available = float(contracts) * float(contract_size)
        take = min(remaining, available)
        if take <= 0:
            continue
        filled += take
        quote += take * float(price)
        remaining -= take
        if remaining <= 1e-12:
            break

    return VirtualIocFill(
        qty=filled,
        avg_price=quote / filled if filled > 0 else 0.0,
        requested_qty=requested_qty,
        limit_price=limit_price,
    )


def immediate_roundtrip_cost_bps(
    book: LiveBook,
    *,
    direction: int,
    entry_price: float,
    qty: float,
    contract_size: float,
) -> float:
    filled, exit_vwap = exit_depth_for_qty(
        book,
        direction=direction,
        qty=qty,
        contract_size=contract_size,
    )
    if filled + 1e-12 < qty or exit_vwap <= 0 or entry_price <= 0:
        return math.inf
    return max(0.0, -signed_move_bps(direction, entry_price, exit_vwap))


def executable_edge_ok(
    residual_bps: float,
    cost_bps: float,
    min_net_bps: float,
    min_ratio: float,
) -> tuple[bool, float]:
    required = max(cost_bps + min_net_bps, cost_bps * min_ratio)
    return abs(residual_bps) >= required, required


def entry_slippage_bps(direction: int, book: LiveBook, fill_price: float) -> float:
    best = book.ask if direction > 0 else book.bid
    if best <= 0 or fill_price <= 0:
        return math.inf
    return max(0.0, signed_move_bps(direction, best, fill_price))
