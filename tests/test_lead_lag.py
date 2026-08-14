import math

from mexc_tick_scalper.lead_lag import LeadLagModel, mexc_to_binance_symbol


def test_symbol_mapping():
    assert mexc_to_binance_symbol("BTC_USDT") == "BTCUSDT"


def test_lead_lag_detects_binance_up_lead():
    model = LeadLagModel(
        horizon_ms=200,
        baseline_seconds=5,
        min_edge_bps=2.0,
        min_binance_move_bps=1.0,
        max_age_ms=500,
    )
    base_ts = 1_000_000
    for i in range(6):
        ts = base_ts + i * 100
        model.update_binance(bid=99.99, ask=100.01, ts_ms=ts)
        model.update_mexc_price(price=100.0, ts_ms=ts)

    model.update_binance(bid=100.07, ask=100.09, ts_ms=base_ts + 700)
    model.update_mexc_price(price=100.01, ts_ms=base_ts + 700)
    snap = model.snapshot(now_ms=base_ts + 700)

    assert snap.ready
    assert snap.direction == 1
    assert snap.edge_bps > 2.0
    assert snap.binance_move_bps > snap.mexc_move_bps


def test_lead_lag_rejects_when_mexc_already_caught_up():
    model = LeadLagModel(
        horizon_ms=200,
        baseline_seconds=5,
        min_edge_bps=0.1,
        min_binance_move_bps=0.1,
        max_age_ms=500,
    )
    base_ts = 2_000_000
    for i in range(6):
        ts = base_ts + i * 100
        model.update_binance(bid=99.99, ask=100.01, ts_ms=ts)
        model.update_mexc_price(price=100.0, ts_ms=ts)

    model.update_binance(bid=100.04, ask=100.06, ts_ms=base_ts + 700)
    model.update_mexc_price(price=100.06, ts_ms=base_ts + 700)
    snap = model.snapshot(now_ms=base_ts + 700)

    assert not snap.ready
    assert snap.reason in {"leader_direction_mismatch", "mexc_not_lagging", "edge_too_small"}


def test_stale_quotes_are_not_tradable():
    model = LeadLagModel(max_age_ms=100)
    ts = 3_000_000
    for i in range(4):
        model.update_binance(bid=99.99, ask=100.01, ts_ms=ts + i * 10)
        model.update_mexc_price(price=100.0, ts_ms=ts + i * 10)
    snap = model.snapshot(now_ms=ts + 500)
    assert not snap.ready
    assert snap.reason == "stale_quotes"
