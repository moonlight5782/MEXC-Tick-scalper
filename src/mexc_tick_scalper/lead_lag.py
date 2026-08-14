from __future__ import annotations

import asyncio
import json
import math
import time
from collections import deque
from dataclasses import dataclass

import aiohttp

BINANCE_FUTURES_WS = "wss://fstream.binance.com/public/ws"
BINANCE_FUTURES_REST = "https://fapi.binance.com"


def mexc_to_binance_symbol(symbol: str) -> str:
    return symbol.replace("_", "").upper()


@dataclass(frozen=True, slots=True)
class LeadLagSnapshot:
    ready: bool
    direction: int
    edge_bps: float
    raw_gap_bps: float
    baseline_gap_bps: float
    binance_move_bps: float
    mexc_move_bps: float
    binance_mid: float
    mexc_mid: float
    age_ms: float
    reason: str


class LeadLagModel:
    """Cross-exchange lead/lag estimator with a rolling Binance/MEXC basis baseline."""

    def __init__(
        self,
        *,
        horizon_ms: int = 250,
        baseline_seconds: float = 20.0,
        min_edge_bps: float = 2.0,
        min_binance_move_bps: float = 1.0,
        max_age_ms: float = 500.0,
    ) -> None:
        self.horizon_ms = max(50, int(horizon_ms))
        self.baseline_ms = max(2_000, int(float(baseline_seconds) * 1000))
        self.min_edge_bps = max(0.0, float(min_edge_bps))
        self.min_binance_move_bps = max(0.0, float(min_binance_move_bps))
        self.max_age_ms = max(50.0, float(max_age_ms))
        self.binance: deque[tuple[int, float]] = deque(maxlen=20_000)
        self.mexc: deque[tuple[int, float]] = deque(maxlen=20_000)
        self.gaps: deque[tuple[int, float]] = deque(maxlen=20_000)

    @staticmethod
    def _trim(rows: deque[tuple[int, float]], cutoff_ms: int) -> None:
        while rows and rows[0][0] < cutoff_ms:
            rows.popleft()

    @staticmethod
    def _mid(bid: float, ask: float) -> float:
        return (bid + ask) / 2.0 if ask > bid > 0 else 0.0

    @staticmethod
    def _past_price(rows: deque[tuple[int, float]], target_ms: int) -> float | None:
        if not rows:
            return None
        chosen = rows[0][1]
        for ts_ms, price in rows:
            if ts_ms > target_ms:
                break
            chosen = price
        return chosen

    def update_binance(self, *, bid: float, ask: float, ts_ms: int | None = None) -> None:
        mid = self._mid(bid, ask)
        if mid <= 0:
            return
        ts = int(ts_ms if ts_ms is not None else time.time() * 1000)
        self.binance.append((ts, mid))
        self._trim(self.binance, ts - max(self.baseline_ms, self.horizon_ms) * 2)
        self._update_gap(ts)

    def update_mexc(self, *, bid: float, ask: float, ts_ms: int | None = None) -> None:
        mid = self._mid(bid, ask)
        if mid <= 0:
            return
        ts = int(ts_ms if ts_ms is not None else time.time() * 1000)
        self.mexc.append((ts, mid))
        self._trim(self.mexc, ts - max(self.baseline_ms, self.horizon_ms) * 2)
        self._update_gap(ts)

    def _update_gap(self, ts_ms: int) -> None:
        if not self.binance or not self.mexc:
            return
        b_mid = self.binance[-1][1]
        m_mid = self.mexc[-1][1]
        if b_mid <= 0 or m_mid <= 0:
            return
        gap = math.log(b_mid / m_mid) * 10_000.0
        self.gaps.append((ts_ms, gap))
        self._trim(self.gaps, ts_ms - self.baseline_ms)

    def snapshot(self, *, now_ms: int | None = None) -> LeadLagSnapshot:
        now = int(now_ms if now_ms is not None else time.time() * 1000)
        if not self.binance or not self.mexc or len(self.gaps) < 3:
            return LeadLagSnapshot(False, 0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, math.inf, "warming_up")

        b_ts, b_mid = self.binance[-1]
        m_ts, m_mid = self.mexc[-1]
        age_ms = float(max(now - b_ts, now - m_ts))
        if age_ms > self.max_age_ms:
            return LeadLagSnapshot(False, 0, 0.0, 0.0, 0.0, 0.0, 0.0, b_mid, m_mid, age_ms, "stale_quotes")

        raw_gap = math.log(b_mid / m_mid) * 10_000.0
        baseline_values = [gap for ts, gap in self.gaps if ts <= now - self.horizon_ms]
        if len(baseline_values) < 3:
            baseline_values = [gap for _, gap in self.gaps]
        baseline = sum(baseline_values) / len(baseline_values)
        edge = raw_gap - baseline

        target = now - self.horizon_ms
        b_old = self._past_price(self.binance, target)
        m_old = self._past_price(self.mexc, target)
        if not b_old or not m_old:
            return LeadLagSnapshot(False, 0, edge, raw_gap, baseline, 0.0, 0.0, b_mid, m_mid, age_ms, "warming_horizon")

        b_move = math.log(b_mid / b_old) * 10_000.0
        m_move = math.log(m_mid / m_old) * 10_000.0
        direction = 1 if edge > 0 else -1 if edge < 0 else 0

        if abs(edge) < self.min_edge_bps:
            reason = "edge_too_small"
            ready = False
        elif abs(b_move) < self.min_binance_move_bps:
            reason = "leader_move_too_small"
            ready = False
        elif direction * b_move <= 0:
            reason = "leader_direction_mismatch"
            ready = False
        elif abs(b_move) <= abs(m_move):
            reason = "mexc_not_lagging"
            ready = False
        else:
            reason = "lead_lag_confirmed"
            ready = True

        return LeadLagSnapshot(
            ready, direction, edge, raw_gap, baseline, b_move, m_move, b_mid, m_mid, age_ms, reason
        )


class BinanceBookTickerFeed:
    """Public Binance USD-M bookTicker stream. No API key is required."""

    def __init__(self, symbol: str, model: LeadLagModel) -> None:
        self.symbol = mexc_to_binance_symbol(symbol)
        self.model = model
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self.last_error: str | None = None

    async def start(self) -> None:
        if self._task is None or self._task.done():
            self._stop.clear()
            self._task = asyncio.create_task(self._run())

    async def close(self) -> None:
        self._stop.set()
        task = self._task
        self._task = None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def _run(self) -> None:
        stream = f"{self.symbol.lower()}@bookTicker"
        url = f"{BINANCE_FUTURES_WS}/{stream}"
        timeout = aiohttp.ClientTimeout(total=None, sock_connect=10, sock_read=30)
        while not self._stop.is_set():
            try:
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    async with session.ws_connect(url, heartbeat=15) as ws:
                        self.last_error = None
                        async for msg in ws:
                            if self._stop.is_set():
                                return
                            if msg.type == aiohttp.WSMsgType.TEXT:
                                payload = json.loads(msg.data)
                                bid = float(payload.get("b") or 0)
                                ask = float(payload.get("a") or 0)
                                ts = int(payload.get("E") or payload.get("T") or time.time() * 1000)
                                self.model.update_binance(bid=bid, ask=ask, ts_ms=ts)
                            elif msg.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSED):
                                break
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_error = f"{type(exc).__name__}: {exc}"
                await asyncio.sleep(0.5)


async def fetch_binance_usdm_symbols() -> set[str]:
    timeout = aiohttp.ClientTimeout(total=10)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(f"{BINANCE_FUTURES_REST}/fapi/v1/exchangeInfo") as response:
            response.raise_for_status()
            payload = await response.json()
    out: set[str] = set()
    for row in payload.get("symbols", []):
        if row.get("contractType") != "PERPETUAL" or row.get("status") != "TRADING":
            continue
        if row.get("quoteAsset") != "USDT":
            continue
        symbol = str(row.get("symbol") or "").upper()
        if symbol:
            out.add(symbol)
    return out
