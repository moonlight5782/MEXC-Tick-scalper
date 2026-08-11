import pytest

from mexc_tick_scalper.execution import PaperExecutionAdapter
from mexc_tick_scalper.models import FeeStatus, Tick
from mexc_tick_scalper.risk import PositionPlan
from mexc_tick_scalper.state import EligibilityState, apply_fee_status
from mexc_tick_scalper.trading import TradingController


@pytest.mark.asyncio
async def test_controller_opens_and_exits_on_first_adverse_tick():
    execution = PaperExecutionAdapter()
    controller = TradingController(execution, reversal_ticks=1)
    eligibility = apply_fee_status(
        EligibilityState("TEST_USDT"),
        FeeStatus(maker=0.0, taker=0.0, source="test"),
        1,
    )
    plan = PositionPlan(200.0, 5.0, 10, 50.0, 0.5, 1.0)

    opened = await controller.open_from_signal(
        symbol="TEST_USDT",
        direction=1,
        best_price=100.0,
        plan=plan,
        eligibility=eligibility,
    )
    assert opened

    assert await controller.on_tick(Tick("TEST_USDT", 101.0, 1.0, 1, 1000)) is False
    assert await controller.on_tick(Tick("TEST_USDT", 102.0, 1.0, 1, 2000)) is False
    assert await controller.on_tick(Tick("TEST_USDT", 101.5, 1.0, -1, 3000)) is True
    assert await execution.get_position("TEST_USDT") is None


@pytest.mark.asyncio
async def test_fee_pause_blocks_new_entry_but_does_not_force_close():
    execution = PaperExecutionAdapter()
    controller = TradingController(execution, reversal_ticks=1)
    eligible = apply_fee_status(
        EligibilityState("TEST_USDT"),
        FeeStatus(maker=0.0, taker=0.0, source="test"),
        1,
    )
    plan = PositionPlan(200.0, 5.0, 10, 50.0, 0.5, 1.0)
    assert await controller.open_from_signal(
        symbol="TEST_USDT",
        direction=1,
        best_price=100.0,
        plan=plan,
        eligibility=eligible,
    )

    paused = apply_fee_status(
        eligible,
        FeeStatus(maker=0.0, taker=0.0001, source="test"),
        2,
    )
    assert not paused.can_open_new_position
    # Existing trade remains until the strategy's tick exit fires.
    assert await controller.on_tick(Tick("TEST_USDT", 101.0, 1.0, 1, 1000)) is False
