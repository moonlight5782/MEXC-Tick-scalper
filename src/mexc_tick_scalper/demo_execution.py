from __future__ import annotations

from dataclasses import dataclass
import os
from typing import Any

import aiohttp

from .execution import ExecutionAdapter, OrderFill, OrderSide, PositionSnapshot


DEMO_HOST = "futures.testnet.mexc.com"


@dataclass(slots=True)
class DemoSessionConfig:
    base_url: str = f"https://{DEMO_HOST}"
    cookie: str = ""
    csrf_token: str = ""
    uid: str = ""
    timeout_seconds: float = 8.0

    @classmethod
    def from_env(cls) -> "DemoSessionConfig":
        return cls(
            base_url=os.getenv("MEXC_DEMO_BASE_URL", f"https://{DEMO_HOST}"),
            cookie=os.getenv("MEXC_DEMO_COOKIE", ""),
            csrf_token=os.getenv("MEXC_DEMO_CSRF_TOKEN", ""),
            uid=os.getenv("MEXC_DEMO_UID", ""),
            timeout_seconds=float(os.getenv("MEXC_DEMO_TIMEOUT_SECONDS", "8")),
        )

    def validate(self) -> None:
        # Hard safety boundary: this adapter must never point to the live trading site.
        if DEMO_HOST not in self.base_url:
            raise ValueError(
                f"demo adapter refuses non-testnet base_url: {self.base_url!r}"
            )
        if not self.cookie:
            raise ValueError("MEXC_DEMO_COOKIE is required for demo web-session requests")


class DemoProtocolError(RuntimeError):
    pass


def mexc_open_side(side: OrderSide) -> int:
    return 1 if side is OrderSide.LONG else 3


def mexc_close_side(side: OrderSide) -> int:
    # TradingController passes the action side: SHORT closes a LONG, LONG closes a SHORT.
    return 4 if side is OrderSide.SHORT else 2


class MexcDemoExecutionAdapter(ExecutionAdapter):
    """Cookie/session based adapter for MEXC Futures Demo Trading only.

    The demo website is an explicitly separate testnet environment. This adapter
    is intentionally hard-wired to reject live MEXC hosts. Endpoint shapes match
    the MEXC contract private order/position model, while authentication is carried
    by the browser demo session cookie rather than an API key.

    Because MEXC can change website-session requirements without notice, callers
    must run `demo-check` before placing any demo order. Failures are fail-closed.
    """

    def __init__(self, config: DemoSessionConfig | None = None) -> None:
        self.config = config or DemoSessionConfig.from_env()
        self.config.validate()

    def _headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json",
            "Cookie": self.config.cookie,
            "Origin": self.config.base_url,
            "Referer": f"{self.config.base_url}/futures/BTC_USDT",
            "User-Agent": "Mozilla/5.0 MEXC-Tick-scalper-demo/0.1",
        }
        if self.config.csrf_token:
            headers["X-CSRF-Token"] = self.config.csrf_token
        if self.config.uid:
            headers["X-UID"] = self.config.uid
        return headers

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> Any:
        url = f"{self.config.base_url.rstrip('/')}/{path.lstrip('/')}"
        timeout = aiohttp.ClientTimeout(total=self.config.timeout_seconds)
        async with aiohttp.ClientSession(timeout=timeout, headers=self._headers()) as session:
            async with session.request(method, url, params=params, json=json) as response:
                text = await response.text()
                if response.status >= 400:
                    raise DemoProtocolError(f"HTTP {response.status} from demo: {text[:300]}")
                try:
                    payload = await response.json(content_type=None)
                except Exception as exc:  # pragma: no cover - defensive around remote format drift
                    raise DemoProtocolError(f"non-JSON response from demo: {text[:300]}") from exc

        if not isinstance(payload, dict):
            raise DemoProtocolError(f"unexpected demo response type: {type(payload).__name__}")
        if payload.get("success") is False:
            raise DemoProtocolError(
                f"demo rejected request code={payload.get('code')} message={payload.get('message')}"
            )
        return payload.get("data")

    async def check_session(self) -> bool:
        # Read-only request. A valid authenticated session should return an array.
        data = await self._request("GET", "/api/v1/private/position/open_positions")
        return isinstance(data, list)

    async def get_position(self, symbol: str) -> PositionSnapshot | None:
        data = await self._request(
            "GET",
            "/api/v1/private/position/open_positions",
            params={"symbol": symbol},
        )
        if not data:
            return None
        if not isinstance(data, list):
            raise DemoProtocolError("open_positions data is not a list")

        row = next((x for x in data if str(x.get("symbol", "")).upper() == symbol.upper()), data[0])
        side = OrderSide.LONG if int(row.get("positionType", 0)) == 1 else OrderSide.SHORT
        return PositionSnapshot(
            symbol=str(row.get("symbol", symbol)).upper(),
            side=side,
            qty=float(row.get("holdVol", 0.0)),
            entry_price=float(row.get("holdAvgPrice") or row.get("openAvgPrice") or 0.0),
            leverage=int(row.get("leverage") or 1),
            isolated=int(row.get("openType") or 1) == 1,
        )

    async def _query_fill(
        self,
        order_id: str,
        *,
        symbol: str,
        requested_qty: float,
        side: OrderSide,
        client_order_id: str,
    ) -> OrderFill:
        row = await self._request("GET", f"/api/v1/private/order/get/{order_id}")
        if not isinstance(row, dict):
            raise DemoProtocolError("order query data is not an object")
        fee = float(row.get("takerFee") or 0.0) + float(row.get("makerFee") or 0.0)
        return OrderFill(
            symbol=symbol,
            side=side,
            requested_qty=requested_qty,
            filled_qty=float(row.get("dealVol") or 0.0),
            avg_price=float(row.get("dealAvgPrice") or 0.0),
            fee_usdt=fee,
            order_id=str(row.get("orderId") or order_id),
            client_order_id=str(row.get("externalOid") or client_order_id),
        )

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
        if qty <= 0 or price <= 0:
            raise ValueError("qty and price must be positive")
        body = {
            "symbol": symbol,
            "price": price,
            "vol": qty,
            "leverage": leverage,
            "side": mexc_open_side(side),
            "type": 3,  # IOC / transact-or-cancel instantly
            "openType": 1,  # isolated
            "externalOid": client_order_id[:32],
            "reduceOnly": False,
        }
        order_id = await self._request("POST", "/api/v1/private/order/submit", json=body)
        if order_id is None:
            raise DemoProtocolError("demo order submit returned no order id")
        return await self._query_fill(
            str(order_id),
            symbol=symbol,
            requested_qty=qty,
            side=side,
            client_order_id=client_order_id,
        )

    async def close_market_reduce_only(
        self,
        *,
        symbol: str,
        qty: float,
        side: OrderSide,
        client_order_id: str,
    ) -> OrderFill:
        if qty <= 0:
            raise ValueError("qty must be positive")
        position = await self.get_position(symbol)
        if position is None:
            raise DemoProtocolError(f"no demo position to close for {symbol}")
        body = {
            "symbol": symbol,
            "price": 0,
            "vol": min(qty, position.qty),
            "leverage": position.leverage,
            "side": mexc_close_side(side),
            "type": 5,  # market
            "openType": 1,
            "positionId": None,
            "externalOid": client_order_id[:32],
            "reduceOnly": True,
        }
        order_id = await self._request("POST", "/api/v1/private/order/submit", json=body)
        if order_id is None:
            raise DemoProtocolError("demo close submit returned no order id")
        return await self._query_fill(
            str(order_id),
            symbol=symbol,
            requested_qty=qty,
            side=side,
            client_order_id=client_order_id,
        )
