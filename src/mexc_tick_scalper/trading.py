from __future__ import annotations

from dataclasses import dataclass
import uuid

from .execution import ExecutionAdapter, OrderSide, PositionSnapshot
from .exit_logic import TickExitTracker
from .models import Tick
from .risk import PositionPlan
from .state import EligibilityState


@dataclass(slots=True)
class ManagedPosition:
    snapshot: PositionSnapshot
    exit_tracker: TickExitTracker


class TradingController:
    """Exchange-agnostic live/paper control loop.

    Fee/state checks happen before entry. Once a position exists, fee changes block
    new entries but do not override the normal tick exit of the already-open trade.
    """

    def __init__(self, execution: ExecutionAdapter, reversal_ticks: int = 1) -> None:
        self.execution = execution
        self.reversal_ticks = reversal_ticks
        self.positions: dict[str, ManagedPosition] = {}

    async def reconcile(self, symbol: str) -> PositionSnapshot | None:
        remote = await self.execution.get_position(symbol)
        if remote is None:
            self.positions.pop(symbol, None)
            return None

        current = self.positions.get(symbol)
        if current is None:
            side = 1 if remote.side is OrderSide.LONG else -1
            self.positions[symbol] = ManagedPosition(
                snapshot=remote,
                exit_tracker=TickExitTracker(
                    side=side,
                    entry_price=remote.entry_price,
                    reversal_ticks=self.reversal_ticks,
                ),
            )
        else:
            current.snapshot = remote
        return remote

    async def open_from_signal(
        self,
        *,
        symbol: str,
        direction: int,
        best_price: float,
        plan: PositionPlan,
        eligibility: EligibilityState,
    ) -> bool:
        if not eligibility.can_open_new_position:
            return False
        if symbol in self.positions:
            return False
        if direction not in (1, -1):
            return False

        side = OrderSide.LONG if direction == 1 else OrderSide.SHORT
        fill = await self.execution.open_ioc(
            symbol=symbol,
            side=side,
            price=best_price,
            qty=plan.qty,
            leverage=plan.leverage,
            client_order_id=f"entry-{uuid.uuid4().hex}",
        )
        if fill.filled_qty <= 0:
            return False

        snapshot = PositionSnapshot(
            symbol=symbol,
            side=side,
            qty=fill.filled_qty,
            entry_price=fill.avg_price,
            leverage=plan.leverage,
            isolated=True,
        )
        self.positions[symbol] = ManagedPosition(
            snapshot=snapshot,
            exit_tracker=TickExitTracker(
                side=direction,
                entry_price=fill.avg_price,
                reversal_ticks=self.reversal_ticks,
            ),
        )
        return True

    async def on_tick(self, tick: Tick) -> bool:
        managed = self.positions.get(tick.symbol)
        if managed is None:
            return False
        if not managed.exit_tracker.on_tick(tick.price):
            return False

        position = managed.snapshot
        close_side = OrderSide.SHORT if position.side is OrderSide.LONG else OrderSide.LONG
        fill = await self.execution.close_market_reduce_only(
            symbol=tick.symbol,
            qty=position.qty,
            side=close_side,
            client_order_id=f"exit-{uuid.uuid4().hex}",
        )
        if fill.filled_qty >= position.qty:
            self.positions.pop(tick.symbol, None)
        else:
            position.qty -= fill.filled_qty
        return True
