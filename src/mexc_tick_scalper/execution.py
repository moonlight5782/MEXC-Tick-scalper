from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Protocol


class OrderSide(str, Enum):
    LONG = "long"
    SHORT = "short"


@dataclass(slots=True)
class OrderFill:
    symbol: str
    side: OrderSide
    requested_qty: float
    filled_qty: float
    avg_price: float
    fee_usdt: float
    order_id: str
    client_order_id: str
    position_id: str | None = None


@dataclass(slots=True)
class PositionSnapshot:
    symbol: str
    side: OrderSide
    qty: float
    entry_price: float
    leverage: int
    isolated: bool
    position_id: str | None = None
    liquidation_price: float | None = None
    unrealized_pnl: float | None = None


class ExecutionAdapter(Protocol):
    async def open_ioc(
        self,
        *,
        symbol: str,
        side: OrderSide,
        price: float,
        qty: float,
        leverage: int,
        client_order_id: str,
    ) -> OrderFill: ...

    async def close_market_reduce_only(
        self,
        *,
        symbol: str,
        qty: float,
        side: OrderSide,
        client_order_id: str,
    ) -> OrderFill: ...

    async def get_position(self, symbol: str) -> PositionSnapshot | None: ...


class PaperExecutionAdapter:
    """Deterministic non-live adapter used to validate the control loop.

    It intentionally assumes full fills and zero fees. Real web-session execution
    must replace it before live_enabled can ever be honored.
    """

    def __init__(self) -> None:
        self.positions: dict[str, PositionSnapshot] = {}
        self._counter = 0

    def _id(self) -> str:
        self._counter += 1
        return f"paper-{self._counter}"

    async def open_ioc(self, *, symbol: str, side: OrderSide, price: float, qty: float, leverage: int, client_order_id: str) -> OrderFill:
        if symbol in self.positions:
            raise RuntimeError(f"position already open for {symbol}")
        if qty <= 0 or price <= 0:
            raise ValueError("qty and price must be positive")
        position_id = self._id()
        self.positions[symbol] = PositionSnapshot(symbol, side, qty, price, leverage, True, position_id=position_id)
        return OrderFill(symbol, side, qty, qty, price, 0.0, self._id(), client_order_id, position_id=position_id)

    async def close_market_reduce_only(self, *, symbol: str, qty: float, side: OrderSide, client_order_id: str) -> OrderFill:
        position = self.positions.get(symbol)
        if position is None:
            raise RuntimeError(f"no open position for {symbol}")
        closed = min(qty, position.qty)
        if closed <= 0:
            raise ValueError("qty must be positive")
        position_id = position.position_id
        if closed >= position.qty:
            del self.positions[symbol]
        else:
            position.qty -= closed
        return OrderFill(symbol, side, qty, closed, position.entry_price, 0.0, self._id(), client_order_id, position_id=position_id)

    async def get_position(self, symbol: str) -> PositionSnapshot | None:
        return self.positions.get(symbol)
