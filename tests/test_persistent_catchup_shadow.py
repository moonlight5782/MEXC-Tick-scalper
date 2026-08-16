from mexc_tick_scalper.microspread import MicroSpreadSnapshot
from mexc_tick_scalper.prelive_persistent_catchup_shadow import (
    Signal,
    delayed_catchup_entry_ok,
    impulse_retention_fraction,
)


def _signal(**overrides):
    base = dict(
        signal_id="x",
        ts_ms=1,
        symbol="TEST_USDT",
        direction=1,
        residual_bps=10.0,
        threshold_bps=3.0,
        noise_bps=1.0,
        spread_bps=1.0,
        leader_advantage_bps=8.0,
        binance_move_bps=8.0,
        mexc_move_bps=0.0,
        binance_price=100.08,
        mexc_price=100.0,
    )
    base.update(overrides)
    return Signal(**base)


def test_snapshot_price_aliases_match_mid_fields():
    snap = MicroSpreadSnapshot(
        ready=True,
        direction=1,
        edge_bps=1.0,
        raw_gap_bps=1.0,
        baseline_gap_bps=0.0,
        binance_move_bps=2.0,
        mexc_move_bps=0.0,
        binance_mid=101.25,
        mexc_mid=100.75,
        age_ms=1.0,
        binance_age_ms=1.0,
        mexc_age_ms=1.0,
        threshold_bps=0.5,
        reason="test",
    )
    assert snap.binance_price == snap.binance_mid == 101.25
    assert snap.mexc_price == snap.mexc_mid == 100.75


def test_impulse_retention_rejects_leader_retrace():
    s = _signal()
    assert impulse_retention_fraction(1, s.binance_price, s.binance_move_bps, 100.02) < 0.70
    ok, reason, _, _ = delayed_catchup_entry_ok(
        signal=s,
        current_residual_bps=8.0,
        current_binance_price=100.02,
        current_spread_bps=1.0,
        min_residual_retention=0.55,
        min_impulse_retention=0.70,
        min_remaining_edge_bps=2.5,
        min_edge_after_spread_bps=1.5,
    )
    assert not ok
    assert reason == "leader_retraced_before_entry"


def test_persistent_residual_and_held_leader_can_enter():
    s = _signal()
    ok, reason, residual_ret, impulse_ret = delayed_catchup_entry_ok(
        signal=s,
        current_residual_bps=7.0,
        current_binance_price=100.075,
        current_spread_bps=1.0,
        min_residual_retention=0.55,
        min_impulse_retention=0.70,
        min_remaining_edge_bps=2.5,
        min_edge_after_spread_bps=1.5,
    )
    assert ok
    assert reason == "exploitable"
    assert residual_ret >= 0.55
    assert impulse_ret >= 0.70


def test_entry_rejects_edge_that_does_not_clear_spread_buffer():
    s = _signal(residual_bps=6.0)
    ok, reason, _, _ = delayed_catchup_entry_ok(
        signal=s,
        current_residual_bps=3.0,
        current_binance_price=100.08,
        current_spread_bps=2.0,
        min_residual_retention=0.50,
        min_impulse_retention=0.70,
        min_remaining_edge_bps=2.5,
        min_edge_after_spread_bps=1.5,
    )
    assert not ok
    assert reason == "remaining_edge_too_small"
