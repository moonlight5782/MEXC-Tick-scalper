from __future__ import annotations

import asyncio
import json
import math
import time
from dataclasses import dataclass

import aiohttp

from .lead_lag import BINANCE_FUTURES_WS
from .live_zero_fee_universe import LiveZeroFeeContract

MEXC_FUTURES_WS = "wss://contract.mexc.com/edge"


@dataclass(frozen=True, slots=True)
class LiveBook:
    bid: float
    ask: float
    recv_ms: int
    exchange_ts_ms: int
    bids: tuple[tuple[float, float], ...] = ()
    asks: tuple[tuple[float, float], ...] = ()

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2.0

    @property
    def spread_bps(self) -> float:
        mid = self.mid
        return (self.ask - self.bid) / mid * 10_000.0 if mid > 0 else math.inf


class EventBinanceBookTickerFeed:
    """Multi-symbol Binance USD-M bookTicker feed that wakes the strategy per update."""

    def __init__(self, contracts: list[LiveZeroFeeContract], models: dict[str, object], wake: asyncio.Event) -> None:
        self.contracts = contracts
        self.models = models
        self.wake = wake
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self.last_error: str | None = None
        self.quotes = 0
        self.last_quote_ms = 0
        self._mexc_by_binance = {row.binance_symbol: row.mexc_symbol for row in contracts}

    async def start(self) -> None:
        self._stop.clear()
        self._task = asyncio.create_task(self._run())

    async def close(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _run(self) -> None:
        timeout = aiohttp.ClientTimeout(total=None, sock_connect=10, sock_read=35)
        params = [f"{row.binance_symbol.lower()}@bookTicker" for row in self.contracts]
        while not self._stop.is_set():
            try:
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    async with session.ws_connect(BINANCE_FUTURES_WS, heartbeat=15) as ws:
                        await ws.send_json({"method": "SUBSCRIBE", "params": params, "id": 1})
                        self.last_error = None
                        async for msg in ws:
                            if self._stop.is_set():
                                return
                            if msg.type != aiohttp.WSMsgType.TEXT:
                                if msg.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSED):
                                    break
                                continue
                            payload = json.loads(msg.data)
                            if "result" in payload and payload.get("id") == 1:
                                continue
                            b_symbol = str(payload.get("s") or "").upper()
                            mexc_symbol = self._mexc_by_binance.get(b_symbol)
                            if mexc_symbol is None:
                                continue
                            try:
                                bid = float(payload.get("b") or 0)
                                ask = float(payload.get("a") or 0)
                            except (TypeError, ValueError):
                                continue
                            if not (ask > bid > 0):
                                continue
                            recv_ms = int(time.time() * 1000)
                            self.models[mexc_symbol].update_binance(bid=bid, ask=ask, ts_ms=recv_ms)
                            self.quotes += 1
                            self.last_quote_ms = recv_ms
                            self.wake.set()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_error = f"{type(exc).__name__}: {exc}"
                await asyncio.sleep(0.25)


class EventMexcDepthFeed:
    """MEXC full-depth feed using local arrival timestamps for latency comparison.

    ``books`` contains the latest snapshot and ``previous_books`` the immediately
    preceding distinct snapshot. Paper execution can therefore require displayed
    IOC liquidity to survive more than one real LIVE depth update instead of
    assuming a single snapshot is fillable.
    """

    def __init__(
        self,
        symbols: list[str],
        models: dict[str, object] | None,
        wake: asyncio.Event,
        *,
        shard_size: int = 20,
        depth_limit: int = 5,
        ws_url: str = MEXC_FUTURES_WS,
    ) -> None:
        self.symbols = list(symbols)
        self.models = models or {}
        self.wake = wake
        self.shard_size = max(1, int(shard_size))
        self.depth_limit = max(1, int(depth_limit))
        self.ws_url = str(ws_url)
        self.books: dict[str, LiveBook] = {}
        self.previous_books: dict[str, LiveBook] = {}
        self._tasks: list[asyncio.Task] = []
        self._stop = asyncio.Event()
        self.last_errors: dict[int, str] = {}
        self.updates = 0
        self.last_update_ms = 0

    @staticmethod
    def _chunks(items: list[str], size: int) -> list[list[str]]:
        return [items[i : i + size] for i in range(0, len(items), size)]

    @staticmethod
    def _parse_book(payload: dict, recv_ms: int) -> tuple[str, LiveBook] | None:
        if not str(payload.get("channel") or "").startswith("push.depth"):
            return None
        symbol = str(payload.get("symbol") or "").upper()
        data = payload.get("data") or {}
        if not symbol or not isinstance(data, dict):
            return None
        bids = data.get("bids") or []
        asks = data.get("asks") or []
        try:
            bid_levels = tuple(sorted(
                ((float(row[0]), float(row[1])) for row in bids
                 if isinstance(row, (list, tuple)) and len(row) >= 2 and float(row[1]) > 0),
                key=lambda row: row[0], reverse=True,
            ))
            ask_levels = tuple(sorted(
                ((float(row[0]), float(row[1])) for row in asks
                 if isinstance(row, (list, tuple)) and len(row) >= 2 and float(row[1]) > 0),
                key=lambda row: row[0],
            ))
        except (TypeError, ValueError):
            return None
        if not bid_levels or not ask_levels:
            return None
        bid = bid_levels[0][0]
        ask = ask_levels[0][0]
        if not (ask > bid > 0):
            return None
        exchange_ts = int(payload.get("ts") or recv_ms)
        if exchange_ts < 10_000_000_000:
            exchange_ts *= 1000
        return symbol, LiveBook(
            bid=bid, ask=ask, recv_ms=recv_ms, exchange_ts_ms=exchange_ts,
            bids=bid_levels, asks=ask_levels,
        )

    async def start(self) -> None:
        self._stop.clear()
        for shard_id, rows in enumerate(self._chunks(self.symbols, self.shard_size)):
            self._tasks.append(asyncio.create_task(self._run_shard(shard_id, rows)))

    async def close(self) -> None:
        self._stop.set()
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._tasks.clear()

    async def _run_shard(self, shard_id: int, symbols: list[str]) -> None:
        timeout = aiohttp.ClientTimeout(total=None, sock_connect=10, sock_read=30)
        while not self._stop.is_set():
            try:
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    async with session.ws_connect(self.ws_url, heartbeat=None) as ws:
                        for symbol in symbols:
                            await ws.send_json({
                                "method": "sub.depth.full",
                                "param": {"symbol": symbol, "limit": self.depth_limit},
                                "gzip": False,
                            })
                        self.last_errors.pop(shard_id, None)
                        next_ping = time.monotonic() + 10.0
                        while not self._stop.is_set():
                            timeout_s = max(0.05, next_ping - time.monotonic())
                            try:
                                msg = await asyncio.wait_for(ws.receive(), timeout=timeout_s)
                            except TimeoutError:
                                await ws.send_json({"method": "ping"})
                                next_ping = time.monotonic() + 10.0
                                continue
                            if time.monotonic() >= next_ping:
                                await ws.send_json({"method": "ping"})
                                next_ping = time.monotonic() + 10.0

                            if msg.type == aiohttp.WSMsgType.TEXT:
                                payload = json.loads(msg.data)
                            elif msg.type == aiohttp.WSMsgType.BINARY:
                                payload = json.loads(msg.data.decode("utf-8"))
                            elif msg.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSED):
                                break
                            else:
                                continue

                            recv_ms = int(time.time() * 1000)
                            parsed = self._parse_book(payload, recv_ms)
                            if parsed is None:
                                continue
                            symbol, book = parsed
                            if symbol not in self.symbols:
                                continue
                            old = self.books.get(symbol)
                            if old is not None and old.recv_ms != book.recv_ms:
                                self.previous_books[symbol] = old
                            self.books[symbol] = book
                            model = self.models.get(symbol)
                            if model is not None:
                                model.update_mexc(bid=book.bid, ask=book.ask, ts_ms=recv_ms)
                            self.updates += 1
                            self.last_update_ms = recv_ms
                            self.wake.set()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_errors[shard_id] = f"{type(exc).__name__}: {exc}"
                await asyncio.sleep(0.25)
