from mexc_tick_scalper.testnet_latency_arb_product import (
    LagStats,
    LatencyModel,
    expected_remaining_edge_bps,
    latency_economics_ok,
)


def test_expected_remaining_edge_goes_to_zero_at_lag_lifetime():
    assert expected_remaining_edge_bps(residual_bps=12.0, entry_latency_ms=0.0, median_lifetime_ms=300.0) == 12.0
    assert expected_remaining_edge_bps(residual_bps=12.0, entry_latency_ms=150.0, median_lifetime_ms=300.0) == 6.0
    assert expected_remaining_edge_bps(residual_bps=12.0, entry_latency_ms=300.0, median_lifetime_ms=300.0) == 0.0


def test_latency_economics_requires_window_longer_than_entry_plus_exit():
    ok, remaining, why = latency_economics_ok(
        residual_bps=20.0,
        roundtrip_cost_bps=3.0,
        median_lifetime_ms=250.0,
        survival_rate=0.9,
        entry_latency_ms=120.0,
        total_latency_budget_ms=300.0,
        min_survival_rate=0.6,
        min_profit_reserve_bps=2.0,
    )
    assert not ok
    assert remaining == 0.0
    assert why == "lag_window_shorter_than_entry_plus_exit_latency"


def test_latency_economics_requires_positive_profit_reserve_after_latency():
    ok, remaining, why = latency_economics_ok(
        residual_bps=12.0,
        roundtrip_cost_bps=4.0,
        median_lifetime_ms=400.0,
        survival_rate=0.8,
        entry_latency_ms=200.0,
        total_latency_budget_ms=350.0,
        min_survival_rate=0.6,
        min_profit_reserve_bps=3.0,
    )
    assert not ok
    assert remaining == 6.0
    assert why == "remaining_edge_below_cost_plus_profit_reserve"


def test_latency_economics_accepts_only_surviving_profitable_window():
    ok, remaining, why = latency_economics_ok(
        residual_bps=20.0,
        roundtrip_cost_bps=3.0,
        median_lifetime_ms=600.0,
        survival_rate=0.8,
        entry_latency_ms=150.0,
        total_latency_budget_ms=350.0,
        min_survival_rate=0.6,
        min_profit_reserve_bps=2.0,
    )
    assert ok
    assert remaining == 15.0
    assert why == "ok"


def test_lag_stats_survival_rate_uses_observed_completed_windows():
    stats = LagStats()
    stats.completed_ms.extend([100.0, 300.0, 500.0, 700.0])
    assert stats.median_ms() == 400.0
    assert stats.survival_rate(300.0) == 0.75
    assert stats.survival_rate(600.0) == 0.25


def test_latency_model_uses_observed_p75_and_never_artificial_wait():
    model = LatencyModel(bootstrap_entry_ms=150.0, bootstrap_exit_ms=150.0)
    model.entry_samples.extend([100.0, 180.0, 220.0, 300.0])
    model.exit_samples.extend([120.0, 150.0, 200.0, 240.0])
    assert model.entry_ms() == 220.0
    assert model.exit_ms() == 200.0
    assert model.total_budget_ms(50.0) == 470.0
