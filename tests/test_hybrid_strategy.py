from mexc_tick_scalper.hybrid_strategy import AsymmetricExitPolicy, MicrostructureSignal, MicrostructureSnapshot
from mexc_tick_scalper.models import Tick


def _snap(direction=0, confidence=0.0):
    return MicrostructureSnapshot(direction, confidence, 5.0, 0.5, 0.0, 0.0, 5)


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


def test_unproven_loser_cuts_after_two_adverse_price_changes():
    policy = AsymmetricExitPolicy(side=1, entry_price=100.0, early_adverse_changes=2, winner_arm_bps=5.0)
    assert policy.on_tick(price=99.99, liquidation_price=None, signal=_snap(), age_seconds=0.2) is None
    assert policy.on_tick(price=99.98, liquidation_price=None, signal=_snap(), age_seconds=0.4) == "early_adverse_cut"


def test_opposite_signal_cuts_unproven_position():
    policy = AsymmetricExitPolicy(side=1, entry_price=100.0, early_adverse_changes=99)
    assert policy.on_tick(price=100.0, liquidation_price=None, signal=_snap(-1, 0.5), age_seconds=0.2) is None
    assert policy.on_tick(price=100.0, liquidation_price=None, signal=_snap(-1, 0.5), age_seconds=0.5) == "early_signal_flip"


def test_winner_ignores_small_counter_ticks_while_signal_supports():
    policy = AsymmetricExitPolicy(side=1, entry_price=100.0, winner_arm_bps=1.0, winner_pullback_bps=5.0)
    assert policy.on_tick(price=100.02, liquidation_price=None, signal=_snap(1, 0.5), age_seconds=0.5) is None
    assert policy.winner_armed
    assert policy.on_tick(price=100.01, liquidation_price=None, signal=_snap(1, 0.4), age_seconds=0.8) is None
    assert policy.on_tick(price=100.00, liquidation_price=None, signal=_snap(1, 0.4), age_seconds=1.0) is None


def test_winner_requires_confirmed_signal_flip():
    policy = AsymmetricExitPolicy(
        side=1,
        entry_price=100.0,
        winner_arm_bps=1.0,
        flip_confidence=0.3,
        winner_flip_confirmations=3,
    )
    assert policy.on_tick(price=100.02, liquidation_price=None, signal=_snap(1, 0.5), age_seconds=0.5) is None
    assert policy.on_tick(price=100.02, liquidation_price=None, signal=_snap(-1, 0.5), age_seconds=0.8) is None
    assert policy.on_tick(price=100.02, liquidation_price=None, signal=_snap(-1, 0.5), age_seconds=0.9) is None
    assert policy.on_tick(price=100.02, liquidation_price=None, signal=_snap(-1, 0.5), age_seconds=1.0) == "winner_signal_flip_confirmed"


def test_winner_flip_confirmation_resets_when_signal_recovers():
    policy = AsymmetricExitPolicy(
        side=1,
        entry_price=100.0,
        winner_arm_bps=1.0,
        flip_confidence=0.3,
        winner_flip_confirmations=3,
    )
    assert policy.on_tick(price=100.02, liquidation_price=None, signal=_snap(1, 0.5), age_seconds=0.5) is None
    assert policy.on_tick(price=100.02, liquidation_price=None, signal=_snap(-1, 0.5), age_seconds=0.8) is None
    assert policy.on_tick(price=100.02, liquidation_price=None, signal=_snap(1, 0.4), age_seconds=0.9) is None
    assert policy.on_tick(price=100.02, liquidation_price=None, signal=_snap(-1, 0.5), age_seconds=1.0) is None
    assert policy.winner_opposite_count == 1


def test_winner_pullback_requires_signal_fade():
    policy = AsymmetricExitPolicy(side=1, entry_price=100.0, winner_arm_bps=1.0, winner_pullback_bps=1.0)
    assert policy.on_tick(price=100.03, liquidation_price=None, signal=_snap(1, 0.5), age_seconds=0.5) is None
    assert policy.on_tick(price=100.01, liquidation_price=None, signal=_snap(1, 0.4), age_seconds=0.8) is None
    assert policy.on_tick(price=100.01, liquidation_price=None, signal=_snap(0, 0.0), age_seconds=1.0) == "winner_pullback_fade"


def test_liquidation_guard_triggers_before_liquidation():
    policy = AsymmetricExitPolicy(side=1, entry_price=100.0, early_adverse_changes=99, liq_buffer_fraction=0.20)
    assert policy.on_tick(price=99.3, liquidation_price=99.0, signal=_snap(), age_seconds=0.2) is None
    assert policy.on_tick(price=99.2, liquidation_price=99.0, signal=_snap(), age_seconds=0.3) == "liquidation_guard"
