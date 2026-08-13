from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass

import aiohttp
import websockets

from .models import Tick, Ticker


def _timestamp_ms(value: object) -> int:
    """Normalize MEXC timestamps that may arrive in seconds or milliseconds."""
    try:
        ts = int(value or 0)
    except (TypeError, ValueError):
        ts = 0
    if ts <= 0:
        return int(time.time() * 1000)
    if ts < 10_000_000_000:  # seconds epoch
        return ts * 1000
    return ts


@dataclass(frozen=True, slots=True)
class OrderBookSnapshot:
    symbol: str
    bids: list[list[float]]
    asks: list[list[float]]
    ts_ms: int


class MexcPublicMarket:
    def __init__(self, rest_base_url: str, websocket_url: str) -> None:
        self.rest_base_url = rest_base_url.rstrip("/")
        self.websocket_url = websocket_url

    async def contracts(self) -> list[dict]:
        url = f"{self.rest_base_url}/api/v1/contract/detail"
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=10) as resp:
                resp.raise_for_status()
                payload = await resp.json()
        data = payload.get("data", payload)
        return data if isinstance(data, list) else []

    async def ticker(self, symbol: str) -> Ticker | None:
        url = f"{self.rest_base_url}/api/v1/contract/ticker"
        async with aiohttp.ClientSession() as session:
            async with session.get(url, params={"symbol": symbol}, timeout=10) as resp:
                resp.raise_for_status()
                payload = await resp.json()
        data = payload.get("data") or {}
        if not data:
            return None
        return Ticker(
            symbol=str(data.get("symbol", symbol)),
            last=float(data.get("lastPrice") or 0),
            bid=float(data.get("bid1") or 0),
            ask=float(data.get("ask1") or 0),
            volume24=float(data.get("volume24") or 0),
            ts_ms=_timestamp_ms(data.get("timestamp")),
        )

    async def depth(self, symbol: str, *, limit: int = 20) -> OrderBookSnapshot | None:
        """Fetch a public L2 snapshot for deterministic OBI/microprice features.

        This deliberately starts with snapshot polling rather than maintaining a
        second bespoke websocket book. Once the feature logic is validated on
        clean Demo runs, the same consumer can be fed by a local websocket book
        tracker without changing the strategy API.
        """
        limit = max(1, min(int(limit), 100))
        url = f"{self.rest_base_url}/api/v1/contract/depth/{symbol}"
        async with aiohttp.ClientSession() as session:
            async with session.get(url, params={"limit": limit}, timeout=5) as resp:
                resp.raise_for_status()
                payload = await resp.json()
        data = payload.get("data", payload) if isinstance(payload, dict) else payload
        if not isinstance(data, dict):
            return None

        def normalize(rows: object) -> list[list[float]]:
            out: list[list[float]] = []
            if not isinstance(rows, list):
                return out
            for row in rows:
                if not isinstance(row, (list, tuple)) or len(row) < 2:
                    continue
                try:
                    price = float(row[0])
                    qty = float(row[1])
                except (TypeError, ValueError):
                    continue
                if price > 0 and qty > 0:
                    out.append([price, qty])
            return out

        bids = normalize(data.get("bids"))
        asks = normalize(data.get("asks"))
        if not bids or not asks:
            return None
        return OrderBookSnapshot(
            symbol=symbol,
            bids=bids,
            asks=asks,
            ts_ms=_timestamp_ms(data.get("timestamp") or payload.get("ts") if isinstance(payload, dict) else 0),
        )

    async def trades(self, symbol: str) -> AsyncIterator[Tick]:
        backoff = 1.0
        while True:
            try:
                async with websockets.connect(self.websocket_url, ping_interval=None, close_timeout=3, max_queue=50_000) as ws:
                    await ws.send(json.dumps({"method": "sub.deal", "param": {"symbol": symbol}, "gzip": False}))
                    backoff = 1.0
                    last_ping = time.monotonic()
                    while True:
                        if time.monotonic() - last_ping > 10:
                            await ws.send(json.dumps({"method": "ping"}))
                            last_ping = time.monotonic()
                        raw = await asyncio.wait_for(ws.recv(), timeout=15)
                        if isinstance(raw, bytes):
                            raw = raw.decode("utf-8")
                        msg = json.loads(raw)
                        if msg.get("channel") != "push.deal":
                            continue
                        data = msg.get("data") or {}
                        rows = data if isinstance(data, list) else [data]
                        for row in rows:
                            price = float(row.get("p") or 0)
                            if price <= 0:
                                continue
                            yield Tick(
                                symbol=symbol,
                                price=price,
                                volume=float(row.get("v") or 0),
                                side=int(row.get("T") or 0),
                                ts_ms=_timestamp_ms(row.get("t") or msg.get("ts")),
                            )
            except asyncio.CancelledError:
                raise
            except Exception:
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2.0, 30.0)
