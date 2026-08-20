import asyncio
import inspect
from argparse import Namespace

import pytest

from mexc_tick_scalper.execution import OrderFill, OrderSide
from mexc_tick_scalper.testnet.execution import TestnetExecutionAdapter
from mexc_tick_scalper.testnet.exit_policy import ExitContext, TestnetExitPolicy
from mexc_tick_scalper.testnet.profit_hold import ProfitHoldPolicy
from mexc_tick_scalper.testnet.risk import BankState, effective_leverage, requested_notional
from mexc_tick_scalper.web_execution import WebExecutionConfig


def _demo_config() -> WebExecutionConfig:
    return WebExecutionConfig(
        auth_token="WEB_test",
        base_url="https://futures.testnet.mexc.com/api/v1",
        origin="https://futures.testnet.mexc.com",
        referer="https://futures.testnet.mexc.com/futures/TEST_USDT",
        write_enabled=True,
        environment="demo",
    )


def _exit_args() -> Namespace:
    return Namespace(
        leader_retrace_exit_bps=1.5,
        reversal_edge_bps=0.75,
        convergence_bps=0.25,
        convergence_fraction=0.25,
        min_catchup_bps=1.0,
        no_progress_ms=3000,
        min_progress_bps=0.5,
        max_hold_ms=15000,
    )


def test_testnet_order_polling_contains_no_software_sleep():
    source = inspect.getsource(TestnetExecutionAdapter._wait_for_order_result)
    assert "asyncio.sleep" not in source
    assert "time.sleep" not in source


def test_testnet_order_polling_reaches_terminal_state_without_sleep(monkeypatch):
    states = iter([
        {"state": 1},
        {"state": 3, "dealVol": 1},
    ])

    async def fake_order(self, symbol, external_id):
        return next(states)

    monkeypatch.setattr(TestnetExecutionAdapter, "_get_order_by_external_id", fake_order)
    adapter = TestnetExecutionAdapter(_demo_config())
    result = asyncio.run(adapter._wait_for_order_result("TEST_USDT", "abc", timeout_seconds=0.5))
    assert result["state"] == 3


def test_confirmed_fill_becomes_position_without_private_reconciliation():
    adapter = TestnetExecutionAdapter(_demo_config())
    fill = OrderFill(
        symbol="TEST_USDT",
        side=OrderSide.LONG,
        requested_qty=2.0,
        filled_qty=1.5,
        avg_price=100.0,
        fee_usdt=0.01,
        order_id="order-1",
        client_order_id="client-1",
        position_id="position-1",
    )
    position = adapter.position_from_fill(
        symbol="TEST_USDT",
        side=OrderSide.LONG,
        fill=fill,
        leverage=100,
    )
    assert position.qty == pytest.approx(1.5)
    assert position.entry_price == pytest.approx(100.0)
    assert position.position_id == "position-1"


def test_profit_hold_arms_on_first_positive_executable_pnl():
    policy = ProfitHoldPolicy(distance_bps=1.5)
    assert policy.update(-0.2) is None
    assert policy.armed is False
    stop = policy.update(0.04)
    assert policy.armed is True
    assert stop is not None
    assert 0 < stop <= 0.04


def test_profit_hold_stop_only_ratchets_up():
    policy = ProfitHoldPolicy(distance_bps=1.5)
    policy.update(3.0)
    first = policy.stop_bps
    policy.update(5.0)
    second = policy.stop_bps
    policy.update(4.0)
    third = policy.stop_bps
    assert first is not None and second is not None and third is not None
    assert second >= first
    assert third >= second


def test_hard_adverse_exit_remains_active_after_profit_hold():
    args = _exit_args()
    policy = ProfitHoldPolicy(distance_bps=1.0)
    policy.update(1.0)
    assert policy.armed
    reason = TestnetExitPolicy().evaluate(
        ExitContext(
            age_ms=100,
            mid_move_bps=-0.02,
            leader_move_bps=5.0,
            residual_bps=5.0,
            signal_direction=1,
            entry_residual_bps=10.0,
            executable_pnl_bps=0.5,
        ),
        args,
        policy,
    )
    assert reason == "mid_adverse_cut"


def test_ordinary_thesis_exit_is_suppressed_after_profit_hold():
    args = _exit_args()
    policy = ProfitHoldPolicy(distance_bps=1.0)
    policy.update(1.0)
    reason = TestnetExitPolicy().evaluate(
        ExitContext(
            age_ms=4000,
            mid_move_bps=1.0,
            leader_move_bps=-10.0,
            residual_bps=-10.0,
            signal_direction=1,
            entry_residual_bps=10.0,
            executable_pnl_bps=1.0,
        ),
        args,
        policy,
    )
    assert reason is None


def test_sizing_preserves_10k_target_and_20_percent_reserve():
    bank = BankState(balance_usdt=100.0)
    requested, margin, reserve = requested_notional(bank, 200)
    assert requested == pytest.approx(10_000.0)
    assert margin == pytest.approx(50.0)
    assert reserve == pytest.approx(50.0)

    requested, margin, reserve = requested_notional(bank, 50)
    assert requested == pytest.approx(4_000.0)
    assert margin == pytest.approx(80.0)
    assert reserve == pytest.approx(20.0)


def test_effective_leverage_is_capped_by_both_contracts():
    assert effective_leverage(500, 200) == 200
    assert effective_leverage(100, 200) == 100
    assert effective_leverage(500, 50) == 50
