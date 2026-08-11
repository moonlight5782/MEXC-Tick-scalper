from __future__ import annotations

from collections import deque
from dataclasses import dataclass

from .models import Tick


def _clamp(value: float, low: float = -1.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def _ema(values: list[float], span: int) -> float:
    if not values:
        return 0.0
    alpha = 2.0 / (max(1, span) + 1.0)
    out = values[0]
    for value in values[1:]:
        out = alpha * value + (1.0 - alpha) * out
    return out


@dataclass(slots=True)
class MicrostructureSnapshot:
    direction: int
    confidence: float
    trade_rate: float
    buy_ratio: float
    cvd_norm: float
    momentum_bps: float
    price_changes: int


class MicrostructureSignal:
    """Lightweight signal fusion inspired by smallfish.

    Uses only data we already receive reliably from MEXC trade WS:
    micro-momentum, CVD/trade-flow and activity. Full L2 OBI is intentionally
    excluded until we maintain a proper multi-level order book locally.
    """

    def __init__(self, window_seconds: float = 5.0, min_trade_rate: float = 0.5) -> None:
        self.window_ms = max(1000, int(window_seconds * 1000))
        self.min_trade_rate = max(0.0, float(min_trade_rate))
        self.ticks: deque[Tick] = deque(maxlen=20_000)

    def update(self, tick: Tick) -> MicrostructureSnapshot:
        self.ticks.append(tick)
        cutoff = tick.ts_ms - self.window_ms
        while self.ticks and self.ticks[0].ts_ms < cutoff:
            self.ticks.popleft()

        rows = list(self.ticks)
        if len(rows) < 4:
            return MicrostructureSnapshot(0, 0.0, 0.0, 0.5, 0.0, 0.0, 0)

        elapsed = max(0.25, (rows[-1].ts_ms - rows[0].ts_ms) / 1000.0)
        trade_rate = len(rows) / elapsed

        buy_vol = sum(max(0.0, t.volume) for t in rows if t.side == 1)
        sell_vol = sum(max(0.0, t.volume) for t in rows if t.side == 2)
        total_vol = buy_vol + sell_vol
        buy_ratio = buy_vol / total_vol if total_vol > 0 else 0.5
        cvd_norm = (buy_vol - sell_vol) / total_vol if total_vol > 0 else 0.0

        prices: list[float] = []
        for t in rows:
            if not prices or t.price != prices[-1]:
                prices.append(t.price)
        price_changes = max(0, len(prices) - 1)
        if len(prices) < 3:
            return MicrostructureSnapshot(0, 0.0, trade_rate, buy_ratio, cvd_norm, 0.0, price_changes)

        fast = _ema(prices[-8:], span=3)
        slow = _ema(prices[-16:], span=7)
        momentum_bps = ((fast - slow) / slow) * 10_000 if slow > 0 else 0.0

        # smallfish-inspired fusion: momentum + CVD/trade-flow, with activity as a gate.
        momentum_score = _clamp(momentum_bps / 2.0)
        cvd_score = _clamp(cvd_norm)
        flow_score = _clamp((buy_ratio - 0.5) / 0.20)
        raw = 0.45 * momentum_score + 0.35 * cvd_score + 0.20 * flow_score

        if trade_rate < self.min_trade_rate:
            return MicrostructureSnapshot(0, 0.0, trade_rate, buy_ratio, cvd_norm, momentum_bps, price_changes)

        confidence = min(1.0, abs(raw))
        direction = 1 if raw > 0 else -1 if raw < 0 else 0
        return MicrostructureSnapshot(direction, confidence, trade_rate, buy_ratio, cvd_norm, momentum_bps, price_changes)


@dataclass(slots=True)
class HoldUntilAgainstExit:
    """Hold while movement is favorable; exit on confirmed adverse price changes.

    Equal-price trades are ignored. A favorable change resets the adverse counter.
    A liquidation guard closes before the exchange liquidation boundary is reached.
    """

    side: int
    entry_price: float
    adverse_changes: int = 3
    liq_buffer_fraction: float = 0.20
    last_price: float | None = None
    adverse_count: int = 0
    extreme_price: float | None = None

    def __post_init__(self) -> None:
        if self.side not in (1, -1):
            raise ValueError("side must be +1 or -1")
        if self.entry_price <= 0:
            raise ValueError("entry_price must be positive")
        if self.adverse_changes < 1:
            raise ValueError("adverse_changes must be >= 1")
        if not 0 < self.liq_buffer_fraction < 1:
            raise ValueError("liq_buffer_fraction must be between 0 and 1")
        self.last_price = self.entry_price
        self.extreme_price = self.entry_price

    def _near_liquidation(self, price: float, liquidation_price: float | None) -> bool:
        if liquidation_price is None or liquidation_price <= 0:
            return False
        if self.side == 1 and liquidation_price < self.entry_price:
            trigger = liquidation_price + (self.entry_price - liquidation_price) * self.liq_buffer_fraction
            return price <= trigger
        if self.side == -1 and liquidation_price > self.entry_price:
            trigger = liquidation_price - (liquidation_price - self.entry_price) * self.liq_buffer_fraction
            return price >= trigger
        return False

    def on_price(self, price: float, liquidation_price: float | None = None) -> str | None:
        if price <= 0:
            return None
        if self._near_liquidation(price, liquidation_price):
            return "liquidation_guard"

        assert self.last_price is not None
        assert self.extreme_price is not None
        if price == self.last_price:
            return None

        delta = price - self.last_price
        favorable = delta > 0 if self.side == 1 else delta < 0
        if favorable:
            self.adverse_count = 0
            if (self.side == 1 and price > self.extreme_price) or (self.side == -1 and price < self.extreme_price):
                self.extreme_price = price
        else:
            self.adverse_count += 1

        self.last_price = price
        if self.adverse_count >= self.adverse_changes:
            return "confirmed_adverse_move"
        return None
