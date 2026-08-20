from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from ..execution import OrderFill, OrderSide, PositionSnapshot
from ..lead_lag_strategy import LeadLagGate
from ..microspread import MicroSpreadModel
from ..microspread_feed import EventBinanceBookTickerFeed, EventMexcDepthFeed, LiveBook
from ..web_execution import MexcWebError, WebExecutionConfig
from .execution import TestnetExecutionAdapter
from .exit_policy import ExitContext, TestnetExitPolicy
from .market_math import (
    entry_slippage_bps,
    executable_edge_ok,
    immediate_roundtrip_cost_bps,
    signed_move_bps,
    virtual_ioc_fill,
)
from .models import CandidateView
from .profit_hold import ProfitHoldPolicy
from .reporting import SessionStats, TradeReporter
from .risk import BankState, demo_ioc_price, effective_leverage, requested_notional
from .signals import TradeSignal, arrival_entry_ok, directional_move_bps
from .snapshot import event_key, valid_snapshot


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


class TestnetTradingEngine:
    """Trade exactly one selected pair using LIVE alpha and Demo execution."""

    def __init__(
        self,
        *,
        args,
        selected: CandidateView,
        execution_config: WebExecutionConfig,
        console,
    ) -> None:
        self.args = args
        self.selected = selected
        self.execution_config = execution_config
        self.console = console
        self.bank = BankState()
        self.stats = SessionStats()
        self.exit_policy = TestnetExitPolicy()
        self.reporter = TradeReporter(Path(args.testnet_output), console)

    def _build_model(self) -> MicroSpreadModel:
        return MicroSpreadModel(
            horizon_ms=self.args.micro_horizon_ms,
            baseline_seconds=self.args.baseline_seconds,
            baseline_exclusion_ms=self.args.baseline_exclusion_ms,
            min_edge_bps=0.0,
            min_binance_move_bps=0.0,
            max_binance_age_ms=self.args.max_binance_age_ms,
            max_mexc_age_ms=self.args.max_mexc_age_ms,
        )

    def _build_gate(self) -> LeadLagGate:
        return LeadLagGate(
            noise_window_ms=self.args.noise_window_ms,
            residual_noise_multiplier=self.args.residual_noise_multiplier,
            binance_noise_multiplier=self.args.binance_noise_multiplier,
            min_edge_bps=self.args.min_edge_bps,
            min_net_edge_bps=self.args.min_net_edge_bps,
            spread_ratio=self.args.edge_to_spread_ratio,
            min_binance_move_bps=self.args.min_binance_move_bps,
            min_leader_advantage_bps=self.args.min_leader_advantage_bps,
            min_lead_ratio=self.args.min_lead_ratio,
            confirm_updates=self.args.confirm_updates,
            confirm_ms=self.args.confirm_ms,
            rearm_fraction=self.args.rearm_fraction,
        )

    async def _close_position(
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

    async def _try_open(
        self,
        *,
        adapter: TestnetExecutionAdapter,
        model: MicroSpreadModel,
        gate: LeadLagGate,
        book: LiveBook,
        now_ms: int,
        leverage: int,
        demo_detail: dict,
    ) -> ActivePosition | None:
        snapshot = model.snapshot(now_ms=now_ms, threshold_bps=0.0)
        if not valid_snapshot(snapshot):
            return None

        decision = gate.observe(
            self.selected.symbol,
            snapshot,
            book.spread_bps,
            now_ms,
            event_key=event_key(model),
        )
        strength = abs(decision.residual_bps) / max(decision.threshold_bps, 1e-12)
        if not (
            decision.ready
            and strength >= self.args.min_signal_strength_ratio
            and abs(decision.residual_bps) >= self.args.min_absolute_residual_bps
        ):
            return None

        self.stats.signals += 1
        signal_ms = int(time.time() * 1000)
        signal = TradeSignal(
            signal_id=f"tn-{self.stats.signals}-{signal_ms}",
            ts_ms=signal_ms,
            symbol=self.selected.symbol,
            direction=decision.direction,
            residual_bps=decision.residual_bps,
            threshold_bps=decision.threshold_bps,
            noise_bps=decision.noise_bps,
            spread_bps=book.spread_bps,
            leader_advantage_bps=decision.leader_advantage_bps,
            binance_move_bps=decision.binance_move_bps,
            mexc_move_bps=decision.mexc_move_bps,
            binance_price=snapshot.binance_mid,
            mexc_price=snapshot.mexc_mid,
        )
        self.console.print(
            f"SIGNAL #{self.stats.signals} {signal.symbol} "
            f"{'LONG' if signal.direction > 0 else 'SHORT'} "
            f"residual={signal.residual_bps:+.2f}bps strength={strength:.2f}x"
        )

        arrival_ok, arrival_reason, residual_retention, impulse_retention = arrival_entry_ok(
            signal=signal,
            current_residual_bps=snapshot.edge_bps,
            current_binance_price=snapshot.binance_mid,
            current_spread_bps=book.spread_bps,
            min_remaining_edge_bps=self.args.min_absolute_residual_bps,
            min_edge_after_spread_bps=self.args.min_edge_after_spread_bps,
        )
        if not arrival_ok:
            self.stats.expired += 1
            self.console.print(
                f"ENTRY REJECT {signal.symbol} {arrival_reason} "
                f"residual_retention={residual_retention:.1%} impulse_retention={impulse_retention:.1%}"
            )
            return None

        requested, margin, reserve = requested_notional(self.bank, leverage)
        self.console.print(
            f"RISK SIZE {signal.symbol} bank=${self.bank.balance_usdt:.2f} leverage={leverage}x "
            f"margin=${margin:.2f} reserve=${reserve:.2f} requested=${requested:.2f}"
        )

        planned = virtual_ioc_fill(
            book,
            direction=signal.direction,
            target_notional_usdt=requested,
            contract_size=self.selected.candidate.contract.contract_size,
            cross_bps=self.args.ioc_cross_bps,
        )
        planned_notional = planned.qty * planned.avg_price
        planned_slippage = entry_slippage_bps(signal.direction, book, planned.avg_price)
        cost_bps = immediate_roundtrip_cost_bps(
            book,
            direction=signal.direction,
            entry_price=planned.avg_price,
            qty=planned.qty,
            contract_size=self.selected.candidate.contract.contract_size,
        ) if planned.qty > 0 else float("inf")
        edge_ok, required_edge = executable_edge_ok(
            snapshot.edge_bps,
            cost_bps,
            self.args.min_executable_net_edge_bps,
            self.args.min_edge_to_cost_ratio,
        )

        if planned.qty <= 0 or planned_notional < self.args.min_filled_notional_usdt:
            self.stats.nofill += 1
            return None
        if planned_slippage > self.args.max_entry_slippage_bps + 1e-9 or not edge_ok:
            self.stats.expired += 1
            self.console.print(
                f"ENTRY REJECT {signal.symbol} executable economics "
                f"slippage={planned_slippage:.2f}bps cost={cost_bps:.2f}bps required_edge={required_edge:.2f}bps"
            )
            return None

        side = OrderSide.LONG if signal.direction > 0 else OrderSide.SHORT
        demo_best = await adapter.get_best_price(signal.symbol, side)
        limit_price = demo_ioc_price(
            demo_best,
            side,
            self.args.ioc_cross_bps,
            float(demo_detail.get("priceUnit") or 0),
        )
        timing: dict[str, float] = {}
        fill = await adapter.open_ioc(
            symbol=signal.symbol,
            side=side,
            price=limit_price,
            qty=requested / max(demo_best, 1e-12),
            leverage=leverage,
            client_order_id=f"tn-entry-{uuid.uuid4().hex}"[:32],
            timing_marks=timing,
        )
        if fill.filled_qty <= 0:
            self.stats.nofill += 1
            self.console.print(f"TESTNET NO FILL {signal.symbol} requested=${requested:.0f}")
            return None

        entry_confirmed_ms = timing.get("ioc_confirmed_ms", time.time_ns() / 1_000_000.0)
        entry_submit_ms = timing.get("ioc_post_start_ms", float(signal_ms))
        remote = adapter.position_from_fill(
            symbol=signal.symbol,
            side=side,
            fill=fill,
            leverage=leverage,
        )
        management_start_ms = time.time_ns() / 1_000_000.0
        entry_ms = int(entry_confirmed_ms)
        entry_price = float(remote.entry_price or fill.avg_price or demo_best)
        filled_notional = remote.qty * entry_price
        actual_slippage = max(0.0, signed_move_bps(signal.direction, demo_best, entry_price))

        fresh_book = book
        latest = model.snapshot(now_ms=entry_ms, threshold_bps=0.0)
        self.stats.entries += 1
        self.stats.fills.append(filled_notional / max(requested, 1e-12))
        self.stats.signal_to_fill_ms.append(entry_confirmed_ms - signal_ms)

        position = ActivePosition(
            signal=signal,
            remote=remote,
            entry_fill=fill,
            signal_ms=signal_ms,
            entry_ms=entry_ms,
            management_start_ms=int(management_start_ms),
            entry_submit_ms=entry_submit_ms,
            entry_live_mid=fresh_book.mid,
            entry_binance=latest.binance_mid,
            entry_residual_bps=latest.edge_bps,
            demo_entry_best=demo_best,
            entry_price=entry_price,
            entry_slippage_bps=actual_slippage,
            requested_notional=requested,
            filled_notional=filled_notional,
            profit_hold=ProfitHoldPolicy(distance_bps=max(0.0, fresh_book.spread_bps)),
        )

        self.console.print(
            f"TESTNET ENTRY {signal.symbol} requested=${requested:.0f} filled=${filled_notional:.0f} "
            f"margin=${filled_notional/leverage:.2f} lev={leverage}x "
            f"signal_to_fill={entry_confirmed_ms-signal_ms:.1f}ms "
            f"fill_to_management={management_start_ms-entry_confirmed_ms:.3f}ms "
            f"Demo best={demo_best:.10g} fill={entry_price:.10g} "
            f"slippage={actual_slippage:.2f}bps entry_fee=${fill.fee_usdt:.4f}"
        )

        abort_reason: str | None = None
        latest_book = fresh_book
        latest_snapshot = latest
        if not valid_snapshot(latest_snapshot):
            abort_reason = "post_fill_invalid_live_snapshot"
        elif entry_ms - latest_book.recv_ms > self.args.max_book_age_ms:
            abort_reason = "post_fill_stale_live_book"
        elif filled_notional < self.args.min_filled_notional_usdt:
            abort_reason = "actual_fill_too_small"
        elif actual_slippage > self.args.max_entry_slippage_bps + 1e-9:
            abort_reason = "actual_entry_slippage"
        else:
            post_ok, post_reason, _, _ = arrival_entry_ok(
                signal=signal,
                current_residual_bps=latest_snapshot.edge_bps,
                current_binance_price=latest_snapshot.binance_mid,
                current_spread_bps=latest_book.spread_bps,
                min_remaining_edge_bps=self.args.min_absolute_residual_bps,
                min_edge_after_spread_bps=self.args.min_edge_after_spread_bps,
            )
            if not post_ok:
                abort_reason = f"arrival_{post_reason}"

        if abort_reason is not None:
            self.console.print(
                f"POST-FILL GUARD {signal.symbol} reason={abort_reason}; submit close immediately"
            )
            await self._close_position(
                adapter,
                position,
                reason=abort_reason,
                exit_decision_ms=time.time_ns() / 1_000_000.0,
            )
            return None

        return position

    async def _manage_position(
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
        mid_move = directional_move_bps(
            position.signal.direction,
            position.entry_live_mid,
            book.mid,
        )
        leader_move = directional_move_bps(
            position.signal.direction,
            position.entry_binance,
            snapshot.binance_mid,
        )

        exit_side = OrderSide.SHORT if position.signal.direction > 0 else OrderSide.LONG
        try:
            demo_exit_best = await adapter.get_best_price(position.signal.symbol, exit_side)
        except MexcWebError:
            decision_ms = time.time_ns() / 1_000_000.0
            self.console.print(
                f"EXIT DECISION {position.signal.symbol} reason=demo_price_unavailable -> close immediately"
            )
            await self._close_position(
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
            f"EXIT DECISION {position.signal.symbol} reason={reason} "
            f"Demo executable={executable_pnl_bps:+.2f}bps -> submit close immediately"
        )
        await self._close_position(
            adapter,
            position,
            reason=reason,
            exit_decision_ms=decision_ms,
        )
        return None

    async def run(self) -> SessionStats:
        symbol = self.selected.symbol
        contract = self.selected.candidate.contract
        self.reporter.prepare(append=bool(self.args.append_output))

        model = self._build_model()
        gate = self._build_gate()
        wake = asyncio.Event()
        binance = EventBinanceBookTickerFeed([contract], {symbol: model}, wake)
        mexc = EventMexcDepthFeed([symbol], {symbol: model}, wake, depth_limit=self.args.depth_limit)
        position: ActivePosition | None = None

        async with TestnetExecutionAdapter(self.execution_config) as adapter:
            existing = await adapter.get_positions()
            if existing:
                labels = ", ".join(
                    f"{item.symbol}:{item.side.value}:{item.qty:g}" for item in existing[:10]
                )
                raise MexcWebError(
                    f"Testnet account already has open position(s): {labels}; refusing to mix sessions"
                )

            demo_detail = await adapter.get_contract_detail(symbol)
            if (
                float(demo_detail.get("contractSize") or 0) <= 0
                or float(demo_detail.get("priceUnit") or 0) <= 0
            ):
                raise MexcWebError(f"{symbol} Testnet contract metadata is invalid")
            leverage = effective_leverage(
                contract.max_leverage,
                int(demo_detail.get("maxLeverage") or self.selected.demo_max_leverage),
            )

            await binance.start()
            await mexc.start()
            self.console.print(
                f"[bold cyan]TESTNET TRADING ENGINE[/bold cyan] {symbol} leverage={leverage}x; "
                "LIVE Binance/MEXC signal + Demo execution; entry=8bps/3x; no software polling sleep"
            )

            warmup_until = time.monotonic() + self.args.warmup_seconds
            session_deadline = time.monotonic() + self.args.session_seconds
            last_report = None

            try:
                while True:
                    now = time.monotonic()
                    entry_budget_open = (
                        now < session_deadline
                        and self.stats.signals < self.args.max_signals
                        and self.stats.closed < self.args.target_closed_trades
                        and self.bank.may_open_new_position
                    )
                    if position is None and not entry_budget_open:
                        break

                    try:
                        await asyncio.wait_for(wake.wait(), timeout=0.25)
                    except TimeoutError:
                        pass
                    wake.clear()

                    now = time.monotonic()
                    now_ms = int(time.time() * 1000)

                    if position is None and entry_budget_open and now >= warmup_until:
                        book = mexc.books.get(symbol)
                        if book is not None and now_ms - book.recv_ms <= self.args.max_book_age_ms:
                            position = await self._try_open(
                                adapter=adapter,
                                model=model,
                                gate=gate,
                                book=book,
                                now_ms=now_ms,
                                leverage=leverage,
                                demo_detail=demo_detail,
                            )

                    if position is not None:
                        position = await self._manage_position(
                            adapter=adapter,
                            position=position,
                            model=model,
                            book=mexc.books.get(symbol),
                            now_ms=int(time.time() * 1000),
                        )

                    report = (
                        self.stats.signals,
                        self.stats.entries,
                        self.stats.expired,
                        self.stats.nofill,
                        self.stats.wins,
                        self.stats.losses,
                        self.stats.flats,
                        round(self.stats.gross_pnl_usdt, 6),
                        round(self.stats.demo_fees_usdt, 6),
                    )
                    if report != last_report:
                        self.console.print("STATE " + self.reporter.summary(self.stats, self.bank))
                        last_report = report

                    if not self.bank.may_open_new_position and position is None:
                        self.console.print(
                            f"[bold red]SESSION KILL SWITCH[/bold red] balance=${self.bank.balance_usdt:.2f} "
                            f"<= ${self.bank.drawdown_stop_balance:.2f}; no new entries"
                        )
                        break
            finally:
                try:
                    if position is not None:
                        self.console.print(
                            f"[bold yellow]SHUTDOWN CLEANUP[/bold yellow] closing Testnet {symbol}"
                        )
                        await self._close_position(
                            adapter,
                            position,
                            reason="shutdown_cleanup",
                            exit_decision_ms=time.time_ns() / 1_000_000.0,
                        )
                        position = None
                finally:
                    await binance.close()
                    await mexc.close()

        self.console.print("\n[bold]FINAL STRUCTURED TESTNET REPORT[/bold]")
        self.console.print(self.reporter.summary(self.stats, self.bank))
        self.console.print(f"CSV: {self.reporter.path.resolve()}")
        return self.stats
