import math

from mexc_tick_scalper.lead_lag import (
    BINANCE_FUTURES_WS,
    LeadLagModel,
    mexc_to_binance_symbol,
)


def test_symbol_mapping():
    assert mexc_to_binance_symbol("BTC_USDT") == "BTCUSDT"


def test_binance_usdm_raw_stream_base_is_official_path():
    assert BINANCE_FUTURES_WS == "wss://fstream.binance.com/ws"


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


def test_recent_gap_fallback_does_not_slice_deque():
    """Regression for Python deque: snapshot warmup fallback must not use gaps[:-1]."""
    model = LeadLagModel(
        horizon_ms=1_000,
        baseline_seconds=2,
        min_edge_bps=0.0,
        min_binance_move_bps=0.0,
        max_age_ms=2_000,
    )
    ts = 4_000_000
    # All gaps are newer than now-horizon, forcing the fallback baseline path.
    for i in range(3):
        now = ts + i * 10
        model.update_binance(bid=99.99 + i * 0.01, ask=100.01 + i * 0.01, ts_ms=now)
        model.update_mexc_price(price=100.0, ts_ms=now)

    snap = model.snapshot(now_ms=ts + 20)

    assert math.isfinite(snap.raw_gap_bps)
    assert math.isfinite(snap.baseline_gap_bps)
    assert math.isfinite(snap.edge_bps)
