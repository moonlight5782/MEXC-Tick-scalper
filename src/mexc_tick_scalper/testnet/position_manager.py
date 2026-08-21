from __future__ import annotations

import time
from dataclasses import dataclass

from ..execution import OrderFill, OrderSide, PositionSnapshot
from ..microspread import MicroSpreadModel
from ..microspread_feed import LiveBook
from ..web_execution import MexcWebError
from .execution import TestnetExecutionAdapter
from .exit_policy import ExitContext, TestnetExitPolicy
from .market_math import signed_move_bps
from .profit_hold import ProfitHoldPolicy
from .reporting import SessionStats, TradeReporter
from .risk import BankState
from .signals import TradeSignal, directional_move_bps
from .snapshot import valid_snapshot


@dataclass(slots=True)
class ActivePosition:
    signal: TradeSignal
    remote: PositionSnapshot
    entry_fill: OrderFill
    signal_ms: int
    entry_ms: int
    management_start_ms: int
    entry_submit_ms: float
    entry_live_mid: float
    entry_binance: float
    entry_residual_bps: float
    demo_entry_best: float
    entry_price: float
    entry_slippage_bps: float
    requested_notional: float
    filled_notional: float
    profit_hold: ProfitHoldPolicy
    mfe_bps: float = 0.0
    mae_bps: float = 0.0


class PositionManager:
    """Manage one confirmed Testnet position from fill until terminal close.

    This block owns position lifecycle only. It does not discover pairs, generate
    entries, size orders, or construct execution/reporting dependencies.
    """

    def __init__(
        self,
        *,
        args,
        bank: BankState,
        stats: SessionStats,
        reporter: TradeReporter,
        console,
        exit_policy: TestnetExitPolicy | None = None,
    ) -> None:
        self.args = args
        self.bank = bank
        self.stats = stats
        self.reporter = reporter
        self.console = console
        self.exit_policy = exit_policy or TestnetExitPolicy()

    async def close_position(
        self,
        adapter: TestnetExecutionAdapter,
        position: ActivePosition,
        *,
        reason: str,
        exit_decision_ms: float | None = None,
    ) -> None:
        decision_ms = exit_decision_ms or time.time_ns() / 1_000_000.0
        exit_submit_ms = time.time_ns() / 1_000_000.0
        close = await adapter.close_position_fully(position.remote)
        self.reporter.record_close(
            stats=self.stats,
            bank=self.bank,
            direction=position.signal.direction,
            qty=position.remote.qty,
            entry_price=position.entry_price,
            filled_notional=position.filled_notional,
            requested_notional=position.requested_notional,
            leverage=position.remote.leverage,
            demo_entry_best=position.demo_entry_best,
            entry_slippage_bps=position.entry_slippage_bps,
            entry_fill=position.entry_fill,
            exit_fill=close.fill,
            signal_ms=position.signal_ms,
            entry_ms=position.entry_ms,
            management_start_ms=position.management_start_ms,
            exit_decision_ms=decision_ms,
            exit_submit_ms=exit_submit_ms,
            exit_fill_ms=close.fill_confirmed_ms,
            exit_reconciled_ms=close.reconciled_ms,
            close_attempts=close.attempts,
            mfe_bps=position.mfe_bps,
            mae_bps=position.mae_bps,
            reason=reason,
            profit_hold_armed=position.profit_hold.armed,
            entry_submit_ms=position.entry_submit_ms,
        )

    async def manage_position(
        self,
        *,
        adapter: TestnetExecutionAdapter,
        position: ActivePosition,
        model: MicroSpreadModel,
        book: LiveBook | None,
        now_ms: int,
    ) -> ActivePosition | None:
        if book is None:
            return position
        snapshot = model.snapshot(now_ms=now_ms, threshold_bps=0.0)
        if not valid_snapshot(snapshot):
            return position

        age_ms = now_ms - position.entry_ms
        mid_move = directional_move_bps(position.signal.direction, position.entry_live_mid, book.mid)
        leader_move = directional_move_bps(position.signal.direction, position.entry_binance, snapshot.binance_mid)

        # This hard decision depends only on current LIVE state and must not wait
        # behind a Demo REST quote request.
        if mid_move <= -self.exit_policy.emergency_mid_adverse_bps:
            decision_ms = time.time_ns() / 1_000_000.0
            self.console.print(
                f"EXIT DECISION {position.signal.symbol} reason=mid_adverse_cut live_mid={mid_move:+.2f}bps "
                "-> submit close immediately"
            )
            await self.close_position(
                adapter,
                position,
                reason="mid_adverse_cut",
                exit_decision_ms=decision_ms,
            )
            return None

        exit_side = OrderSide.SHORT if position.signal.direction > 0 else OrderSide.LONG
        try:
            demo_exit_best = await adapter.get_best_price(position.signal.symbol, exit_side)
        except MexcWebError:
            decision_ms = time.time_ns() / 1_000_000.0
            self.console.print(
                f"EXIT DECISION {position.signal.symbol} reason=demo_price_unavailable -> close immediately"
            )
            await self.close_position(
                adapter,
                position,
                reason="demo_price_unavailable",
                exit_decision_ms=decision_ms,
            )
            return None

        executable_pnl_bps = signed_move_bps(
            position.signal.direction,
            position.entry_price,
            demo_exit_best,
        )
        was_armed = position.profit_hold.armed
        trail = position.profit_hold.update(executable_pnl_bps)
        position.mfe_bps = max(position.mfe_bps, executable_pnl_bps)
        position.mae_bps = min(position.mae_bps, executable_pnl_bps)

        if not was_armed and position.profit_hold.armed:
            self.console.print(
                f"[bold green]PROFIT HOLD ARMED[/bold green] {position.signal.symbol} "
                f"executable={executable_pnl_bps:+.2f}bps stop={trail:+.2f}bps; "
                "ordinary thesis exits suppressed; hard adverse safety remains"
            )

        reason = self.exit_policy.evaluate(
            ExitContext(
                age_ms=age_ms,
                mid_move_bps=mid_move,
                leader_move_bps=leader_move,
                residual_bps=snapshot.edge_bps,
                signal_direction=position.signal.direction,
                entry_residual_bps=position.entry_residual_bps,
                executable_pnl_bps=executable_pnl_bps,
            ),
            self.args,
            position.profit_hold,
        )
        if reason is None:
            return position

        decision_ms = time.time_ns() / 1_000_000.0
        self.console.print(
            f"EXIT DECISION {position.signal.symbol} reason={reason} Demo executable={executable_pnl_bps:+.2f}bps "
            "-> submit close immediately"
        )
        await self.close_position(
            adapter,
            position,
            reason=reason,
            exit_decision_ms=decision_ms,
        )
        return None
