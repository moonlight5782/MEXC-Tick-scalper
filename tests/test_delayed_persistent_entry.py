from mexc_tick_scalper.prelive_latency_diagnostic_v2 import delayed_entry_is_exploitable


def test_delayed_entry_does_not_require_fresh_binance_impulse():
    ok, reason = delayed_entry_is_exploitable(
        signal_direction=1,
        signal_residual_bps=10.0,
        current_residual_bps=6.0,
        current_spread_bps=1.0,
        min_net_edge_bps=0.2,
        min_retention_fraction=0.30,
        min_remaining_edge_bps=2.0,
    )
    assert ok
    assert reason == "exploitable"


def test_delayed_entry_rejects_converged_residual():
    ok, reason = delayed_entry_is_exploitable(
        signal_direction=1,
        signal_residual_bps=10.0,
        current_residual_bps=1.0,
        current_spread_bps=0.9,
        min_net_edge_bps=0.2,
        min_retention_fraction=0.30,
        min_remaining_edge_bps=2.0,
    )
    assert not ok
    assert reason in {"remaining_edge_too_small", "retention_too_low"}


def test_delayed_entry_rejects_reversal():
    ok, reason = delayed_entry_is_exploitable(
        signal_direction=1,
        signal_residual_bps=10.0,
        current_residual_bps=-4.0,
        current_spread_bps=1.0,
        min_net_edge_bps=0.2,
        min_retention_fraction=0.30,
        min_remaining_edge_bps=2.0,
    )
    assert not ok
    assert reason == "residual_reversed"
