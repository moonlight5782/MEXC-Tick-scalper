from mexc_tick_scalper.exit_logic import TickExitTracker
from mexc_tick_scalper.models import FeeStatus
from mexc_tick_scalper.state import EligibilityState, SymbolEligibility, apply_fee_status


def test_fee_pause_is_temporary_and_recovers():
    state = EligibilityState("TEST_USDT")
    apply_fee_status(state, FeeStatus(maker=0.0, taker=0.0, source="test"), 1000)
    assert state.status is SymbolEligibility.ELIGIBLE
    assert state.can_open_new_position

    apply_fee_status(state, FeeStatus(maker=0.0, taker=0.0001, source="test"), 2000)
    assert state.status is SymbolEligibility.PAUSED_FEE
    assert not state.can_open_new_position

    apply_fee_status(state, FeeStatus(maker=0.0, taker=0.0, source="test"), 3000)
    assert state.status is SymbolEligibility.ELIGIBLE
    assert state.can_open_new_position


def test_unknown_fee_blocks_new_entries():
    state = EligibilityState("TEST_USDT")
    apply_fee_status(state, FeeStatus(maker=None, taker=None, source="unknown"), 1000)
    assert state.status is SymbolEligibility.PAUSED_FEE
    assert not state.can_open_new_position


def test_long_exits_on_first_tick_below_best_extreme():
    tracker = TickExitTracker(side=1, entry_price=100.0, reversal_ticks=1)
    assert tracker.on_tick(101.0) is False
    assert tracker.on_tick(102.0) is False
    assert tracker.on_tick(101.5) is True


def test_short_exits_on_first_tick_above_best_extreme():
    tracker = TickExitTracker(side=-1, entry_price=100.0, reversal_ticks=1)
    assert tracker.on_tick(99.0) is False
    assert tracker.on_tick(98.0) is False
    assert tracker.on_tick(98.5) is True


def test_two_tick_confirmation_requires_two_adverse_ticks():
    tracker = TickExitTracker(side=1, entry_price=100.0, reversal_ticks=2)
    assert tracker.on_tick(101.0) is False
    assert tracker.on_tick(100.8) is False
    assert tracker.on_tick(100.7) is True
