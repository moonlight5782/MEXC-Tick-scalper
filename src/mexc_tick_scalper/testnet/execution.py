from __future__ import annotations

import time
import uuid
from typing import Any

from ..execution import OrderFill, OrderSide, PositionSnapshot
from ..web_execution import MexcWebError, MexcWebExecutionAdapter, WebExecutionConfig


class TestnetExecutionAdapter(MexcWebExecutionAdapter):
    """Demo execution with no software-added polling delay on the critical path."""

    def __init__(self, config: WebExecutionConfig) -> None:
        if config.environment != "demo":
            raise MexcWebError("TestnetExecutionAdapter requires environment='demo'")
        super().__init__(config)

    async def _wait_for_order_result(
        self,
        symbol: str,
        client_order_id: str,
        timeout_seconds: float = 1.2,
    ) -> dict[str, Any]:
        """Poll terminal order state back-to-back; HTTP responses are the only wait."""
        deadline = time.monotonic() + timeout_seconds
        last: dict[str, Any] | None = None
        while time.monotonic() < deadline:
            try:
                last = await self._get_order_by_external_id(symbol, client_order_id)
            except MexcWebError:
                last = None
            if last and int(last.get("state") or 0) in (3, 4, 5):
                return last
        if last is not None:
            return last
        raise MexcWebError(f"order {client_order_id} was not observable after submit")

    @staticmethod
    def position_from_fill(
        *,
        symbol: str,
        side: OrderSide,
        fill: OrderFill,
        leverage: int,
    ) -> PositionSnapshot:
        """Begin management from exchange-confirmed fill without a second private GET."""
        if fill.filled_qty <= 0:
            raise MexcWebError("IOC returned no fill")
        return PositionSnapshot(
            symbol=symbol,
            side=side,
            qty=fill.filled_qty,
            entry_price=fill.avg_price,
            leverage=leverage,
            isolated=True,
            position_id=fill.position_id,
            liquidation_price=None,
        )

    async def _close_known_position_without_lookup(
        self,
        position: PositionSnapshot,
        *,
        client_order_id: str,
    ) -> OrderFill:
        """Close known side/qty without a pre-close get_positions request."""
        self._require_write()
        vol = await self._to_contract_vol(position.symbol, position.qty)
        if vol <= 0:
            raise MexcWebError(f"close quantity below minimum contract volume for {position.symbol}")

        external_id = client_order_id[:32]
        payload: dict[str, Any] = {
            "symbol": position.symbol,
            "price": position.entry_price,
            "vol": vol,
            "side": 4 if position.side is OrderSide.LONG else 2,
            "type": 5,
            "openType": 1,
            "externalOid": external_id,
        }
        submitted = await self._request("POST", "/private/order/submit", payload=payload)
        order = await self._wait_for_order_result(position.symbol, external_id)
        filled_qty = await self._from_contract_vol(
            position.symbol,
            float(order.get("dealVol") or 0),
        )
        return OrderFill(
            symbol=position.symbol,
            side=OrderSide.SHORT if position.side is OrderSide.LONG else OrderSide.LONG,
            requested_qty=position.qty,
            filled_qty=filled_qty,
            avg_price=float(order.get("dealAvgPrice") or position.entry_price),
            fee_usdt=float(order.get("takerFee") or 0) + float(order.get("makerFee") or 0),
            order_id=str(order.get("orderId") or (submitted.get("data") if isinstance(submitted, dict) else "")),
            client_order_id=client_order_id,
            position_id=position.position_id,
        )

    async def submit_close(self, position: PositionSnapshot) -> OrderFill:
        client_id = f"tn-exit-{uuid.uuid4().hex}"[:32]
        if position.position_id is not None:
            return await self.close_position_snapshot_reduce_only(
                position,
                client_order_id=client_id,
            )
        return await self._close_known_position_without_lookup(
            position,
            client_order_id=client_id,
        )

    async def find_same_position(self, position: PositionSnapshot) -> PositionSnapshot | None:
        rows = await self.get_positions(position.symbol)
        if position.position_id is not None:
            return next((row for row in rows if row.position_id == position.position_id), None)
        return next((row for row in rows if row.side is position.side), None)

    async def close_position_fully(
        self,
        position: PositionSnapshot,
        *,
        attempts: int = 4,
    ) -> OrderFill:
        """Submit close immediately, then reconcile residual state using network-only polls."""
        current = position
        last_fill: OrderFill | None = None
        for _ in range(max(1, attempts)):
            last_fill = await self.submit_close(current)

            deadline = time.monotonic() + 0.75
            while time.monotonic() < deadline:
                residual = await self.find_same_position(current)
                if residual is None:
                    return last_fill
                current = residual

        residual = await self.find_same_position(current)
        if residual is not None:
            raise MexcWebError(
                f"Demo reduce-only close left residual position {residual.symbol} "
                f"positionId={residual.position_id} qty={residual.qty:g}"
            )
        if last_fill is None:
            raise MexcWebError("Demo close did not submit")
        return last_fill
