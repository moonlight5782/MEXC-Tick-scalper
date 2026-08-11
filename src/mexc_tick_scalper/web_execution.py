from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import time
from dataclasses import dataclass
from typing import Any

import aiohttp

from .execution import OrderFill, OrderSide, PositionSnapshot


class MexcWebError(RuntimeError):
    pass


def _signature(token: str, payload: Any, timestamp_ms: int) -> str:
    """Reverse-engineered browser signature used by MEXC web Futures requests."""
    ts = str(timestamp_ms)
    seed = hashlib.md5((token + ts).encode()).hexdigest()[7:]
    body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    return hashlib.md5((ts + body + seed).encode()).hexdigest()


@dataclass(slots=True)
class WebExecutionConfig:
    auth_token: str
    base_url: str = "https://futures.mexc.com/api/v1"
    origin: str = "https://www.mexc.com"
    referer: str = "https://www.mexc.com/"
    timeout_seconds: float = 5.0
    write_enabled: bool = False

    @classmethod
    def from_env(cls, *, base_url: str | None = None, write_enabled: bool = False) -> "WebExecutionConfig":
        token = os.getenv("MEXC_WEB_TOKEN", "").strip()
        if not token:
            raise MexcWebError("MEXC_WEB_TOKEN is not set")
        if not token.startswith("WEB"):
            raise MexcWebError("MEXC_WEB_TOKEN does not look like a WEB session token")
        return cls(
            auth_token=token,
            base_url=(base_url or os.getenv("MEXC_WEB_BASE_URL") or "https://futures.mexc.com/api/v1").rstrip("/"),
            origin=os.getenv("MEXC_WEB_ORIGIN", "https://www.mexc.com"),
            referer=os.getenv("MEXC_WEB_REFERER", "https://www.mexc.com/"),
            timeout_seconds=float(os.getenv("MEXC_WEB_TIMEOUT_SECONDS", "5")),
            write_enabled=write_enabled,
        )


class MexcWebExecutionAdapter:
    """MEXC Futures browser-session adapter.

    Controller quantities are expressed in base-asset units. MEXC order `vol`
    is contract volume, therefore this adapter converts through contractSize and
    volUnit. Writes are disabled by default.
    """

    def __init__(self, config: WebExecutionConfig) -> None:
        self.config = config
        self._session: aiohttp.ClientSession | None = None
        self._contract_cache: dict[str, dict[str, Any]] = {}

    async def __aenter__(self) -> "MexcWebExecutionAdapter":
        await self._ensure_session()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.close()

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=self.config.timeout_seconds)
            self._session = aiohttp.ClientSession(timeout=timeout)
        return self._session

    def _headers(self, payload: Any | None = None) -> dict[str, str]:
        headers = {
            "accept": "*/*",
            "content-type": "application/json",
            "authorization": self.config.auth_token,
            "origin": self.config.origin,
            "referer": self.config.referer,
            "x-language": "en-US",
            "user-agent": "Mozilla/5.0 MEXC-Tick-scalper/0.1",
        }
        if payload is not None:
            now = int(time.time() * 1000)
            headers["x-mxc-nonce"] = str(now)
            headers["x-mxc-sign"] = _signature(self.config.auth_token, payload, now)
        return headers

    async def _request(self, method: str, path: str, *, params: dict[str, Any] | None = None, payload: Any | None = None) -> Any:
        session = await self._ensure_session()
        url = f"{self.config.base_url}{path}"
        headers = self._headers(payload if method.upper() == "POST" else None)
        async with session.request(method, url, params=params, json=payload, headers=headers) as response:
            text = await response.text()
            try:
                data = json.loads(text)
            except json.JSONDecodeError as exc:
                raise MexcWebError(f"non-JSON response {response.status} from {path}") from exc
            if response.status >= 400:
                raise MexcWebError(f"HTTP {response.status} from {path}: {data}")
            if isinstance(data, dict) and data.get("success") is False:
                raise MexcWebError(f"MEXC error from {path}: code={data.get('code')} message={data.get('message')}")
            return data

    def _require_write(self) -> None:
        if not self.config.write_enabled:
            raise MexcWebError("web execution writes are disabled")

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()

    async def probe(self) -> dict[str, Any]:
        asset = await self._request("GET", "/private/account/asset/USDT")
        positions = await self._request("GET", "/private/position/open_positions")
        return {"asset": asset, "positions": positions}

    async def get_fee_rates(self) -> Any:
        return await self._request("GET", "/private/account/contract/fee_rate")

    async def get_contract_detail(self, symbol: str) -> dict[str, Any]:
        key = symbol.upper()
        if key in self._contract_cache:
            return self._contract_cache[key]
        response = await self._request("GET", "/contract/detail", params={"symbol": symbol})
        data = response.get("data") if isinstance(response, dict) else None
        if isinstance(data, list):
            item = next((x for x in data if str(x.get("symbol", "")).upper() == key), None)
        else:
            item = data if isinstance(data, dict) else None
        if not item:
            raise MexcWebError(f"contract detail missing for {symbol}")
        self._contract_cache[key] = item
        return item

    async def get_best_price(self, symbol: str, side: OrderSide) -> float:
        response = await self._request("GET", f"/contract/depth/{symbol}", params={"limit": 5})
        data = response.get("data", response) if isinstance(response, dict) else response
        if not isinstance(data, dict):
            raise MexcWebError(f"depth response missing for {symbol}")
        levels = data.get("asks") if side is OrderSide.LONG else data.get("bids")
        if not levels:
            raise MexcWebError(f"empty order book for {symbol}")
        return float(levels[0][0])

    async def _contract_size(self, symbol: str) -> float:
        detail = await self.get_contract_detail(symbol)
        size = float(detail.get("contractSize") or 0)
        if size <= 0:
            raise MexcWebError(f"invalid contractSize for {symbol}: {size}")
        return size

    async def _to_contract_vol(self, symbol: str, base_qty: float) -> float:
        if base_qty <= 0:
            raise ValueError("qty must be positive")
        detail = await self.get_contract_detail(symbol)
        contract_size = float(detail.get("contractSize") or 0)
        vol_unit = float(detail.get("volUnit") or 1)
        min_vol = float(detail.get("minVol") or vol_unit)
        if contract_size <= 0 or vol_unit <= 0:
            raise MexcWebError(f"invalid contract sizing metadata for {symbol}")
        raw = base_qty / contract_size
        vol = math.floor((raw + 1e-12) / vol_unit) * vol_unit
        if vol < min_vol:
            return 0.0
        return int(vol) if float(vol).is_integer() else vol

    async def _from_contract_vol(self, symbol: str, vol: float) -> float:
        return float(vol) * await self._contract_size(symbol)

    async def get_position(self, symbol: str) -> PositionSnapshot | None:
        response = await self._request("GET", "/private/position/open_positions", params={"symbol": symbol})
        data = response.get("data", []) if isinstance(response, dict) else []
        if isinstance(data, dict):
            data = [data]
        for item in data or []:
            if str(item.get("symbol", "")).upper() != symbol.upper():
                continue
            hold_vol = float(item.get("holdVol") or 0)
            if hold_vol <= 0:
                continue
            side = OrderSide.LONG if int(item.get("positionType") or 0) == 1 else OrderSide.SHORT
            return PositionSnapshot(
                symbol=symbol,
                side=side,
                qty=await self._from_contract_vol(symbol, hold_vol),
                entry_price=float(item.get("holdAvgPrice") or item.get("openAvgPrice") or 0),
                leverage=int(item.get("leverage") or 1),
                isolated=int(item.get("openType") or 0) == 1,
                position_id=str(item.get("positionId")) if item.get("positionId") is not None else None,
                liquidation_price=float(item.get("liquidatePrice") or 0) or None,
                unrealized_pnl=None,
            )
        return None

    async def _get_order_by_external_id(self, symbol: str, client_order_id: str) -> dict[str, Any] | None:
        response = await self._request("GET", f"/private/order/external/{symbol}/{client_order_id}")
        data = response.get("data") if isinstance(response, dict) else None
        return data if isinstance(data, dict) else None

    async def _wait_for_order_result(self, symbol: str, client_order_id: str, timeout_seconds: float = 1.2) -> dict[str, Any]:
        deadline = time.monotonic() + timeout_seconds
        last: dict[str, Any] | None = None
        while time.monotonic() < deadline:
            try:
                last = await self._get_order_by_external_id(symbol, client_order_id)
            except MexcWebError:
                last = None
            if last and int(last.get("state") or 0) in (3, 4, 5):
                return last
            await asyncio.sleep(0.05)
        if last is not None:
            return last
        raise MexcWebError(f"order {client_order_id} was not observable after submit")

    async def open_ioc(
        self,
        *,
        symbol: str,
        side: OrderSide,
        price: float,
        qty: float,
        leverage: int,
        client_order_id: str,
    ) -> OrderFill:
        self._require_write()
        if price <= 0 or qty <= 0 or leverage <= 0:
            raise ValueError("price, qty and leverage must be positive")
        vol = await self._to_contract_vol(symbol, qty)
        if vol <= 0:
            return OrderFill(symbol, side, qty, 0.0, 0.0, 0.0, "", client_order_id)
        payload = {
            "symbol": symbol,
            "price": price,
            "vol": vol,
            "leverage": leverage,
            "side": 1 if side is OrderSide.LONG else 3,
            "type": 3,
            "openType": 1,
            "externalOid": client_order_id,
        }
        submitted = await self._request("POST", "/private/order/submit", payload=payload)
        order = await self._wait_for_order_result(symbol, client_order_id)
        filled_base_qty = await self._from_contract_vol(symbol, float(order.get("dealVol") or 0))
        return OrderFill(
            symbol=symbol,
            side=side,
            requested_qty=qty,
            filled_qty=filled_base_qty,
            avg_price=float(order.get("dealAvgPrice") or price),
            fee_usdt=float(order.get("takerFee") or 0) + float(order.get("makerFee") or 0),
            order_id=str(order.get("orderId") or (submitted.get("data") if isinstance(submitted, dict) else "")),
            client_order_id=client_order_id,
            position_id=str(order.get("positionId")) if order.get("positionId") is not None else None,
        )

    async def close_market_reduce_only(
        self,
        *,
        symbol: str,
        qty: float,
        side: OrderSide,
        client_order_id: str,
    ) -> OrderFill:
        self._require_write()
        position = await self.get_position(symbol)
        if position is None:
            raise MexcWebError(f"no open position for {symbol}")
        base_qty = min(qty, position.qty)
        vol = await self._to_contract_vol(symbol, base_qty)
        if vol <= 0:
            raise MexcWebError(f"close quantity below minimum contract volume for {symbol}")
        close_side = 4 if position.side is OrderSide.LONG else 2
        payload: dict[str, Any] = {
            "symbol": symbol,
            "price": position.entry_price,
            "vol": vol,
            "side": close_side,
            "type": 5,
            "openType": 1,
            "externalOid": client_order_id,
        }
        if position.position_id is not None:
            payload["positionId"] = position.position_id
        submitted = await self._request("POST", "/private/order/submit", payload=payload)
        order = await self._wait_for_order_result(symbol, client_order_id)
        filled_base_qty = await self._from_contract_vol(symbol, float(order.get("dealVol") or 0))
        return OrderFill(
            symbol=symbol,
            side=side,
            requested_qty=qty,
            filled_qty=filled_base_qty,
            avg_price=float(order.get("dealAvgPrice") or position.entry_price),
            fee_usdt=float(order.get("takerFee") or 0) + float(order.get("makerFee") or 0),
            order_id=str(order.get("orderId") or (submitted.get("data") if isinstance(submitted, dict) else "")),
            client_order_id=client_order_id,
            position_id=position.position_id,
        )
