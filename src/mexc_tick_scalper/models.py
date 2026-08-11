from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Deque


@dataclass(slots=True)
class Tick:
    symbol: str
    price: float
    volume: float
    side: int
    ts_ms: int


@dataclass(slots=True)
class Ticker:
    symbol: str
    last: float
    bid: float
    ask: float
    volume24: float
    ts_ms: int


@dataclass(slots=True)
class FeeStatus:
    maker: float | None = None
    taker: float | None = None
    source: str = "unknown"

    @property
    def zero_confirmed(self) -> bool:
        return self.maker == 0 and self.taker == 0


@dataclass(slots=True)
class ShadowResult:
    symbol: str
    momentum_ticks: int
    reversal_ticks: int
    trades: int
    wins: int
    losses: int
    gross_profit_bps: float
    gross_loss_bps: float
    expectancy_bps: float
    profit_factor: float
    max_drawdown_bps: float


@dataclass
class SymbolState:
    symbol: str
    ticks: Deque[Tick] = field(default_factory=lambda: deque(maxlen=100_000))
    ticker: Ticker | None = None
    fee: FeeStatus = field(default_factory=FeeStatus)

    def append(self, tick: Tick) -> None:
        self.ticks.append(tick)
