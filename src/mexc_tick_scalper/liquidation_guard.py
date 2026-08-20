from __future__ import annotations

import asyncio
import json
import math
import time
from dataclasses import dataclass

import aiohttp

from .live_zero_fee_universe import LiveZeroFeeContract

MEXC_FUTURES_WS = "wss://contract.mexc.com/edge"


@dataclass(frozen=True, slots=True)
class FairPrice:
    price: float
    recv_ms: int
    exchange_ts_ms: int


@dataclass(slots=True)
class LiquidationState:
    symbol: str
    direction: int
    entry_price: float
    qty: float
    notional_usdt: float
    leverage: float
    position_margin_usdt: float
    maintenance_margin_rate: float
    maintenance_margin_usdt: float
    risk_level: int
    liquidation_price: float
    liquidation_distance_bps: float
    liquidated: bool = False
    liquidation_fair_price: float | None = None
    liquidation_seen_ms: int | None = None


def estimated_risk_level(contract: LiveZeroFeeContract, *, qty_base: float, notional_usdt: float) -> int:
    """Conservative public-metadata estimate of MEXC's current risk tier.

    MEXC exposes riskBaseVol/riskIncrVol/riskLevelLimit publicly.  BY_VOLUME is
    evaluated in contracts, while BY_VALUE uses position value.  The private
    risk-limit endpoint remains the authoritative account-specific source.
    """
    if contract.contract_size <= 0:
        return 1
    contracts = max(0.0, qty_base) / contract.contract_size
    metric = notional_usdt if contract.risk_limit_type == "BY_VALUE" else contracts
    base = max(0.0, contract.risk_base_vol)
    incr = max(0.0, contract.risk_incr_vol)
    if base <= 0 or metric <= base or incr <= 0:
        return 1
    level = 1 + int(math.ceil((metric - base) / incr))
    return min(max(1, level), max(1, contract.risk_level_limit))


def estimated_mmr(contract: LiveZeroFeeContract, *, qty_base: float, notional_usdt: float) -> tuple[float, int]:
    level = estimated_risk_level(contract, qty_base=qty_base, notional_usdt=notional_usdt)
    mmr = max(0.0, contract.maintenance_margin_rate) + (level - 1) * max(0.0, contract.risk_incr_mmr)
    return mmr, level


def build_isolated_liquidation_state(
    *,
    contract: LiveZeroFeeContract,
    symbol: str,
    direction: int,
    entry_price: float,
    qty_base: float,
    leverage: float,
) -> LiquidationState | None:
    """Estimate isolated liquidation from MEXC's published formula.

    Liquidation fee is intentionally not guessed because MEXC does not expose it
    in contract/detail and it can differ by contract.  The returned price is
    therefore labelled an estimate; LIVE fair price is used for trigger checks.
    """
    if direction not in (-1, 1) or entry_price <= 0 or qty_base <= 0 or leverage <= 0:
        return None
    notional = entry_price * qty_base
    margin = notional / leverage
    mmr, level = estimated_mmr(contract, qty_base=qty_base, notional_usdt=notional)
    maintenance = notional * mmr
    if direction > 0:
        liq = entry_price + (maintenance - margin) / qty_base
    else:
        liq = entry_price + (margin - maintenance) / qty_base
    if not math.isfinite(liq) or liq <= 0:
        return None
    distance_bps = direction * (entry_price - liq) / entry_price * 10_000.0
    return LiquidationState(
        symbol=symbol,
        direction=direction,
        entry_price=entry_price,
        qty=qty_base,
        notional_usdt=notional,
        leverage=leverage,
        position_margin_usdt=margin,
        maintenance_margin_rate=mmr,
        maintenance_margin_usdt=maintenance,
        risk_level=level,
        liquidation_price=liq,
        liquidation_distance_bps=max(0.0, distance_bps),
    )


def fair_distance_to_liquidation_bps(state: LiquidationState, fair_price: float) -> float:
    if fair_price <= 0:
        return math.inf
    return state.direction * (fair_price - state.liquidation_price) / fair_price * 10_000.0


def fair_price_crossed_liquidation(state: LiquidationState, fair_price: float) -> bool:
    if fair_price <= 0:
        return False
    return fair_price <= state.liquidation_price if state.direction > 0 else fair_price >= state.liquidation_price


class MexcFairPriceFeed:
    """Read-only LIVE MEXC fair-price stream used by the liquidation model."""

    def __init__(self, symbols: list[str], *, ws_url: str = MEXC_FUTURES_WS) -> None:
        self.symbols = list(dict.fromkeys(symbols))
        self.ws_url = ws_url
        self.prices: dict[str, FairPrice] = {}
        self.last_error: str | None = None
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()

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

    def fresh(self, symbol: str, now_ms: int, max_age_ms: int = 2_000) -> FairPrice | None:
        row = self.prices.get(symbol)
        if row is None or now_ms - row.recv_ms > max_age_ms:
            return None
        return row

    async def _run(self) -> None:
        timeout = aiohttp.ClientTimeout(total=None, sock_connect=10, sock_read=30)
        while not self._stop.is_set():
            try:
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    async with session.ws_connect(self.ws_url, heartbeat=None) as ws:
                        for symbol in self.symbols:
                            await ws.send_json({"method": "sub.fair.price", "param": {"symbol": symbol}, "gzip": False})
                        self.last_error = None
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
                            if str(payload.get("channel") or "") != "push.fair.price":
                                continue
                            data = payload.get("data") or {}
                            symbol = str(payload.get("symbol") or data.get("symbol") or "").upper()
                            try:
                                price = float(data.get("price") or 0)
                            except (TypeError, ValueError):
                                continue
                            if symbol not in self.symbols or price <= 0:
                                continue
                            recv_ms = int(time.time() * 1000)
                            exchange_ts = int(payload.get("ts") or recv_ms)
                            if exchange_ts < 10_000_000_000:
                                exchange_ts *= 1000
                            self.prices[symbol] = FairPrice(price, recv_ms, exchange_ts)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_error = f"{type(exc).__name__}: {exc}"
                await asyncio.sleep(0.25)
