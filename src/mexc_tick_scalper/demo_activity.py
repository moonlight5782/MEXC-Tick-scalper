from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

from .market import MexcPublicMarket

TESTNET_WS = "wss://futures.testnet.mexc.com/edge"


@dataclass(slots=True)
class ActivitySample:
    symbol: str
    ticks: int
    price_changes: int
    duration: float

    @property
    def trade_rate(self) -> float:
        return self.ticks / self.duration if self.duration > 0 else 0.0

    @property
    def change_rate(self) -> float:
        return self.price_changes / self.duration if self.duration > 0 else 0.0


async def sample_symbol(symbol: str, seconds: float = 6.0) -> ActivitySample:
    market = MexcPublicMarket("https://futures.testnet.mexc.com", TESTNET_WS)
    start = time.monotonic()
    ticks = 0
    changes = 0
    last_price: float | None = None

    async def collect() -> None:
        nonlocal ticks, changes, last_price
        async for tick in market.trades(symbol):
            ticks += 1
            if last_price is not None and tick.price != last_price:
                changes += 1
            last_price = tick.price
            if time.monotonic() - start >= seconds:
                break

    try:
        await asyncio.wait_for(collect(), timeout=seconds + 2.0)
    except TimeoutError:
        pass
    duration = max(0.001, time.monotonic() - start)
    return ActivitySample(symbol=symbol, ticks=ticks, price_changes=changes, duration=duration)


async def sample_many(symbols: list[str], seconds: float = 6.0) -> dict[str, ActivitySample]:
    results = await asyncio.gather(*(sample_symbol(s, seconds) for s in symbols))
    return {row.symbol: row for row in results}
