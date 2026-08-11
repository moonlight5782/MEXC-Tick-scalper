from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

from .execution import OrderSide
from .market import MexcPublicMarket
from .web_execution import MexcWebError, MexcWebExecutionAdapter, WebExecutionConfig

TESTNET_WS = "wss://futures.testnet.mexc.com/edge"


@dataclass(slots=True)
class ActivitySample:
    symbol: str
    ticks: int
    price_changes: int
    book_samples: int
    book_changes: int
    duration: float

    @property
    def trade_rate(self) -> float:
        return self.ticks / self.duration if self.duration > 0 else 0.0

    @property
    def change_rate(self) -> float:
        return self.price_changes / self.duration if self.duration > 0 else 0.0

    @property
    def book_change_rate(self) -> float:
        return self.book_changes / self.duration if self.duration > 0 else 0.0

    @property
    def activity_rate(self) -> float:
        # Either executed-trade movement or top-of-book movement counts as real
        # testnet activity. Trade movement remains more valuable to the strategy,
        # but a moving book must not be classified as a dead market.
        return self.change_rate + self.book_change_rate


async def sample_symbol(symbol: str, seconds: float = 6.0) -> ActivitySample:
    market = MexcPublicMarket("https://futures.testnet.mexc.com", TESTNET_WS)
    start = time.monotonic()
    ticks = 0
    changes = 0
    book_samples = 0
    book_changes = 0
    last_price: float | None = None
    last_mid: float | None = None

    async def collect_trades() -> None:
        nonlocal ticks, changes, last_price
        async for tick in market.trades(symbol):
            ticks += 1
            if last_price is not None and tick.price != last_price:
                changes += 1
            last_price = tick.price
            if time.monotonic() - start >= seconds:
                break

    async def collect_book() -> None:
        nonlocal book_samples, book_changes, last_mid
        cfg = WebExecutionConfig.demo_from_env(write_enabled=False)
        async with MexcWebExecutionAdapter(cfg) as adapter:
            while time.monotonic() - start < seconds:
                try:
                    ask = await adapter.get_best_price(symbol, OrderSide.LONG)
                    bid = await adapter.get_best_price(symbol, OrderSide.SHORT)
                    if ask > 0 and bid > 0:
                        mid = (ask + bid) / 2.0
                        book_samples += 1
                        if last_mid is not None and abs(mid - last_mid) > 1e-12:
                            book_changes += 1
                        last_mid = mid
                except MexcWebError:
                    pass
                await asyncio.sleep(0.25)

    trade_task = asyncio.create_task(collect_trades())
    book_task = asyncio.create_task(collect_book())
    try:
        await asyncio.wait_for(asyncio.gather(trade_task, book_task), timeout=seconds + 2.0)
    except TimeoutError:
        trade_task.cancel()
        book_task.cancel()
        await asyncio.gather(trade_task, book_task, return_exceptions=True)

    duration = max(0.001, time.monotonic() - start)
    return ActivitySample(
        symbol=symbol,
        ticks=ticks,
        price_changes=changes,
        book_samples=book_samples,
        book_changes=book_changes,
        duration=duration,
    )


async def sample_many(symbols: list[str], seconds: float = 6.0) -> dict[str, ActivitySample]:
    results = await asyncio.gather(*(sample_symbol(s, seconds) for s in symbols))
    return {row.symbol: row for row in results}
