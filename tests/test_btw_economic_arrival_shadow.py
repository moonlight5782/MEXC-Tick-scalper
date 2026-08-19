from types import SimpleNamespace

from mexc_tick_scalper.btw_economic_arrival_shadow import economic_arrival_entry_ok


def _signal(direction: int = 1, residual: float = 50.9):
    return SimpleNamespace(
        direction=direction,
        residual_bps=residual,
        binance_price=100.0,
        binance_move_bps=20.0,
    )


def test_low_retention_large_absolute_edge_survives():
    ok, why, residual_ret, _ = economic_arrival_entry_ok(
        signal=_signal(),
        current_residual_bps=19.95,
        current_binance_price=100.0,
        current_spread_bps=3.55,
        min_residual_retention=0.60,
        min_impulse_retention=0.75,
        min_remaining_edge_bps=8.0,
        min_edge_after_spread_bps=2.0,
    )
    assert ok
    assert why == "absolute_edge_survived"
    assert residual_ret < 0.60


def test_small_absolute_edge_still_rejected():
    ok, why, _, _ = economic_arrival_entry_ok(
        signal=_signal(residual=20.11),
        current_residual_bps=5.6,
        current_binance_price=100.0,
        current_spread_bps=1.11,
        min_residual_retention=0.60,
        min_impulse_retention=0.75,
        min_remaining_edge_bps=8.0,
        min_edge_after_spread_bps=2.0,
    )
    assert not ok
    assert why == "remaining_edge_too_small"


def test_residual_reversal_is_hard_reject():
    ok, why, _, _ = economic_arrival_entry_ok(
        signal=_signal(direction=1),
        current_residual_bps=-10.0,
        current_binance_price=100.0,
        current_spread_bps=1.0,
        min_residual_retention=0.60,
        min_impulse_retention=0.75,
        min_remaining_edge_bps=8.0,
        min_edge_after_spread_bps=2.0,
    )
    assert not ok
    assert why == "residual_reversed"
