from __future__ import annotations

import asyncio
from types import SimpleNamespace

from rich.console import Console

from mexc_tick_scalper.execution import OrderFill, OrderSide, PositionSnapshot
from mexc_tick_scalper.microspread import MicroSpreadSnapshot
from mexc_tick_scalper.microspread_feed import LiveBook
from mexc_tick_scalper.testnet.execution import CloseExecution
from mexc_tick_scalper.testnet.position_manager import ActivePosition, PositionManager
from mexc_tick_scalper.testnet.profit_hold import ProfitHoldPolicy
from mexc_tick_scalper.testnet.reporting import SessionStats
from mexc_tick_scalper.testnet.risk import BankState
from mexc_tick_scalper.testnet.signals import TradeSignal


class StubReporter:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def record_close(self, **kwargs):
        self.calls.append(kwargs)
        return 0.0, 0.0


class StubModel:
    def __init__(self, snapshot: MicroSpreadSnapshot) -> None:
        self._snapshot = snapshot

    def snapshot(self, *, now_ms: int, threshold_bps: float):
        return self._snapshot


class StubExitPolicy:
    emergency_mid_adverse_bps = 0.01

    def __init__(self, reason: str | None = None) -> None:
        self.reason = reason
        self.contexts = []

    def evaluate(self, context, args, profit_hold):
        self.contexts.append((context, profit_hold.armed, profit_hold.stop_bps))
        return self.reason


class StubAdapter:
    def __init__(self, *, best_price: float = 100.0) -> None:
        self.best_price = best_price
        self.best_price_calls = 0
        self.close_calls = 0

    async def get_best_price(self, symbol: str, side: OrderSide) -> float:
        self.best_price_calls += 1
        return self.best_price

    async def close_position_fully(self, position: PositionSnapshot) -> CloseExecution:
        self.close_calls += 1
        fill = OrderFill(
            symbol=position.symbol,
            side=OrderSide.SHORT if position.side is OrderSide.LONG else OrderSide.LONG,
            requested_qty=position.qty,
            filled_qty=position.qty,
            avg_price=position.entry_price,
            fee_usdt=0.0,
            order_id="exit-1",
            client_order_id="exit-client-1",
            position_id=position.position_id,
        )
        return CloseExecution(fill=fill, fill_confirmed_ms=1100.0, reconciled_ms=1101.0, attempts=1)


def _signal() -> TradeSignal:
    return TradeSignal(
        signal_id="sig-1",
        ts_ms=1000,
        symbol="XRP_USDT",
        direction=1,
        residual_bps=10.0,
        threshold_bps=3.0,
        noise_bps=1.0,
        spread_bps=1.0,
        leader_advantage_bps=4.0,
        binance_move_bps=8.0,
        mexc_move_bps=1.0,
        binance_price=100.0,
        mexc_price=100.0,
    )


def _position() -> ActivePosition:
    signal = _signal()
    remote = PositionSnapshot(
        symbol=signal.symbol,
        side=OrderSide.LONG,
        qty=10.0,
        entry_price=100.0,
        leverage=100,
        isolated=True,
        position_id="pos-1",
    )
    entry_fill = OrderFill(
        symbol=signal.symbol,
        side=OrderSide.LONG,
        requested_qty=10.0,
        filled_qty=10.0,
        avg_price=100.0,
        fee_usdt=0.0,
        order_id="entry-1",
        client_order_id="entry-client-1",
        position_id="pos-1",
    )
    return ActivePosition(
        signal=signal,
        remote=remote,
        entry_fill=entry_fill,
        signal_ms=1000,
        entry_ms=1001,
        management_start_ms=1001,
        entry_submit_ms=1000.5,
        entry_live_mid=100.0,
        entry_binance=100.0,
        entry_residual_bps=10.0,
        demo_entry_best=100.0,
        entry_price=100.0,
        entry_slippage_bps=0.0,
        requested_notional=1000.0,
        filled_notional=1000.0,
        profit_hold=ProfitHoldPolicy(distance_bps=1.0),
    )


def _snapshot(*, edge_bps: float = 8.0, binance_mid: float = 100.0) -> MicroSpreadSnapshot:
    return MicroSpreadSnapshot(
        ready=True,
        direction=1,
        edge_bps=edge_bps,
        raw_gap_bps=edge_bps,
        baseline_gap_bps=0.0,
        binance_move_bps=8.0,
        mexc_move_bps=1.0,
        binance_mid=binance_mid,
        mexc_mid=100.0,
        age_ms=0.0,
        binance_age_ms=0.0,
        mexc_age_ms=0.0,
        threshold_bps=0.0,
        reason="microspread_confirmed",
    )


def _manager(exit_policy: StubExitPolicy):
    reporter = StubReporter()
    manager = PositionManager(
        args=SimpleNamespace(),
        bank=BankState(),
        stats=SessionStats(),
        reporter=reporter,
        console=Console(force_terminal=False),
        exit_policy=exit_policy,
    )
    return manager, reporter


def test_emergency_mid_adverse_cut_closes_before_demo_quote_lookup():
    position = _position()
    manager, reporter = _manager(StubExitPolicy())
    adapter = StubAdapter(best_price=100.0)
    model = StubModel(_snapshot())
    book = LiveBook(bid=99.98, ask=99.99, recv_ms=1010, exchange_ts_ms=1010)

    result = asyncio.run(
        manager.manage_position(
            adapter=adapter,
            position=position,
            model=model,
            book=book,
            now_ms=1010,
        )
    )

    assert result is None
    assert adapter.best_price_calls == 0
    assert adapter.close_calls == 1
    assert reporter.calls[0]["reason"] == "mid_adverse_cut"


def test_first_positive_executable_pnl_arms_profit_hold_without_forcing_close():
    position = _position()
    policy = StubExitPolicy(reason=None)
    manager, reporter = _manager(policy)
    adapter = StubAdapter(best_price=100.02)
    model = StubModel(_snapshot(edge_bps=8.0, binance_mid=100.01))
    book = LiveBook(bid=100.00, ask=100.02, recv_ms=1010, exchange_ts_ms=1010)

    result = asyncio.run(
        manager.manage_position(
            adapter=adapter,
            position=position,
            model=model,
            book=book,
            now_ms=1010,
        )
    )

    assert result is position
    assert adapter.best_price_calls == 1
    assert adapter.close_calls == 0
    assert reporter.calls == []
    assert position.profit_hold.armed is True
    assert position.profit_hold.stop_bps is not None
    assert position.mfe_bps > 0.0
    assert policy.contexts and policy.contexts[0][1] is True


def test_close_position_forwards_reconciliation_and_reason_to_reporter():
    position = _position()
    manager, reporter = _manager(StubExitPolicy())
    adapter = StubAdapter()

    asyncio.run(
        manager.close_position(
            adapter,
            position,
            reason="shutdown_cleanup",
            exit_decision_ms=1050.0,
        )
    )

    assert adapter.close_calls == 1
    assert len(reporter.calls) == 1
    call = reporter.calls[0]
    assert call["reason"] == "shutdown_cleanup"
    assert call["exit_decision_ms"] == 1050.0
    assert call["exit_fill_ms"] == 1100.0
    assert call["exit_reconciled_ms"] == 1101.0
    assert call["close_attempts"] == 1
