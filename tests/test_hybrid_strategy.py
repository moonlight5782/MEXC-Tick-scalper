from mexc_tick_scalper.hybrid_strategy import HoldUntilAgainstExit, MicrostructureSignal
from mexc_tick_scalper.models import Tick


def test_microstructure_signal_prefers_buy_flow_and_rising_prices():
    signal = MicrostructureSignal(window_seconds=5.0, min_trade_rate=0.1)
    snap = None
    for i, price in enumerate([100.0, 100.1, 100.2, 100.3, 100.4, 100.5]):
        snap = signal.update(Tick("TEST", price, 10.0, 1, 1_000 + i * 200))
    assert snap is not None
    assert snap.direction == 1
    assert snap.confidence > 0
    assert snap.cvd_norm > 0


def test_microstructure_signal_prefers_sell_flow_and_falling_prices():
    signal = MicrostructureSignal(window_seconds=5.0, min_trade_rate=0.1)
    snap = None
    for i, price in enumerate([100.5, 100.4, 100.3, 100.2, 100.1, 100.0]):
        snap = signal.update(Tick("TEST", price, 10.0, 2, 1_000 + i * 200))
    assert snap is not None
    assert snap.direction == -1
    assert snap.confidence > 0
    assert snap.cvd_norm < 0


def test_hold_until_against_requires_confirmed_adverse_changes():
    tracker = HoldUntilAgainstExit(side=1, entry_price=100.0, adverse_changes=3)
    assert tracker.on_price(100.2) is None
    assert tracker.on_price(100.1) is None
    assert tracker.on_price(100.0) is None
    assert tracker.on_price(99.9) == "confirmed_adverse_move"


def test_favorable_change_resets_adverse_counter():
    tracker = HoldUntilAgainstExit(side=1, entry_price=100.0, adverse_changes=2)
    assert tracker.on_price(100.2) is None
    assert tracker.on_price(100.1) is None
    assert tracker.on_price(100.3) is None
    assert tracker.on_price(100.2) is None
    assert tracker.on_price(100.1) == "confirmed_adverse_move"


def test_liquidation_guard_triggers_before_liquidation():
    tracker = HoldUntilAgainstExit(side=1, entry_price=100.0, adverse_changes=99, liq_buffer_fraction=0.20)
    # Liquidation at 99.0 -> guard threshold = 99.2.
    assert tracker.on_price(99.3, liquidation_price=99.0) is None
    assert tracker.on_price(99.2, liquidation_price=99.0) == "liquidation_guard"
