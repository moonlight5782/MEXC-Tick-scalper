from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator

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
