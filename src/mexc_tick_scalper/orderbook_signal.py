from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Sequence


def _clamp(value: float, low: float = -1.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def _level(level: Sequence[object]) -> tuple[float, float] | None:
    if len(level) < 2:
        return None
    try:
        price = float(level[0])
        qty = float(level[1])
    except (TypeError, ValueError):
        return None
    if price <= 0 or qty <= 0:
        return None
    return price, qty


def _levels(rows: Iterable[Sequence[object]], depth: int) -> list[tuple[float, float]]:
    out: list[tuple[float, float]] = []
    for row in rows:
        parsed = _level(row)
        if parsed is not None:
            out.append(parsed)
        if len(out) >= depth:
            break
    return out


@dataclass(frozen=True, slots=True)
class OrderBookFeatures:
    bid: float
    ask: float
    mid: float
    spread_bps: float
    imbalance: float
    microprice: float
    microprice_edge_bps: float
    bid_depth: float
    ask_depth: float
    pressure: float
    direction: int
    confidence: float

    @property
    def valid(self) -> bool:
        return self.bid > 0 and self.ask > self.bid and self.mid > 0


def analyze_order_book(
    bids: Iterable[Sequence[object]],
    asks: Iterable[Sequence[object]],
    *,
    depth: int = 5,
) -> OrderBookFeatures:
    """Extract low-latency L2 features from a MEXC depth snapshot.

    The design follows the same separation used by mature CLOB systems such as
    Hummingbot: raw order-book state is converted into deterministic features,
    while the trading strategy decides how much authority those features receive.

    `imbalance` is top-N quantity imbalance in [-1, 1].
    `microprice` uses top-of-book queue sizes and moves toward the side with less
    displayed liquidity. `pressure` combines normalized depth imbalance and the
    microprice displacement from the simple mid.
    """
    depth = max(1, int(depth))
    bid_rows = _levels(bids, depth)
    ask_rows = _levels(asks, depth)
    if not bid_rows or not ask_rows:
        return OrderBookFeatures(0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0, 0.0)

    bid, bid_q = bid_rows[0]
    ask, ask_q = ask_rows[0]
    if ask <= bid:
        return OrderBookFeatures(bid, ask, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0, 0.0)

    mid = (bid + ask) / 2.0
    spread_bps = (ask - bid) / mid * 10_000.0

    bid_depth = sum(qty for _, qty in bid_rows)
    ask_depth = sum(qty for _, qty in ask_rows)
    depth_total = bid_depth + ask_depth
    imbalance = (bid_depth - ask_depth) / depth_total if depth_total > 0 else 0.0

    top_total = bid_q + ask_q
    if top_total > 0:
        # Queue-weighted microprice: more bid size pushes fair value toward ask,
        # more ask size pushes it toward bid.
        microprice = (ask * bid_q + bid * ask_q) / top_total
    else:
        microprice = mid
    microprice_edge_bps = (microprice - mid) / mid * 10_000.0 if mid > 0 else 0.0

    # 1 bps of microprice displacement is already meaningful for a tick scalper;
    # keep it bounded so one abnormal snapshot cannot dominate trade-flow.
    micro_score = _clamp(microprice_edge_bps / 1.0)
    pressure = _clamp(0.65 * imbalance + 0.35 * micro_score)
    confidence = min(1.0, abs(pressure))
    direction = 1 if pressure > 0 else -1 if pressure < 0 else 0

    return OrderBookFeatures(
        bid=bid,
        ask=ask,
        mid=mid,
        spread_bps=spread_bps,
        imbalance=imbalance,
        microprice=microprice,
        microprice_edge_bps=microprice_edge_bps,
        bid_depth=bid_depth,
        ask_depth=ask_depth,
        pressure=pressure,
        direction=direction,
        confidence=confidence,
    )


@dataclass(frozen=True, slots=True)
class EntryBookDecision:
    allowed: bool
    reason: str
    confidence_multiplier: float


def book_confirmation(
    *,
    trade_direction: int,
    book: OrderBookFeatures | None,
    veto_confidence: float = 0.45,
    confirm_confidence: float = 0.20,
) -> EntryBookDecision:
    """Use L2 as confirmation/veto, never as a standalone entry trigger.

    This is deliberately conservative: a missing/weak book does not invent a
    trade. Strong disagreement vetoes a trade-flow entry; aligned pressure gives
    a modest confidence boost. This makes adding L2 safer than replacing the
    reconstructed trade-flow signal outright.
    """
    if trade_direction not in (-1, 1):
        return EntryBookDecision(False, "no_trade_direction", 1.0)
    if book is None or not book.valid:
        return EntryBookDecision(True, "book_unavailable", 1.0)

    if book.direction == -trade_direction and book.confidence >= veto_confidence:
        return EntryBookDecision(False, "strong_book_disagreement", 1.0)

    if book.direction == trade_direction and book.confidence >= confirm_confidence:
        boost = 1.0 + min(0.20, 0.20 * book.confidence)
        return EntryBookDecision(True, "book_confirmed", boost)

    return EntryBookDecision(True, "book_neutral", 1.0)
