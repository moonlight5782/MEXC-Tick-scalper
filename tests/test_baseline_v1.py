from argparse import Namespace

from mexc_tick_scalper.baseline_v1 import BASELINE_V1, apply_baseline_v1
from mexc_tick_scalper.microspread import MicroSpreadSnapshot
from mexc_tick_scalper.microspread_feed import LiveBook
from mexc_tick_scalper.prelive_residual_sanity import anomaly_row


def test_baseline_v1_core_execution_parameters_are_frozen():
    assert BASELINE_V1["target_notional_usdt"] == 10000.0
    assert BASELINE_V1["ioc_cross_bps"] == 1.0
    assert BASELINE_V1["max_entry_slippage_bps"] == 1.0
    assert BASELINE_V1["min_absolute_residual_bps"] == 8.0
    assert BASELINE_V1["min_signal_strength_ratio"] == 3.0
    assert BASELINE_V1["min_residual_retention"] == 0.60
    assert BASELINE_V1["min_impulse_retention"] == 0.75
    assert BASELINE_V1["min_executable_net_edge_bps"] == 2.0
    assert BASELINE_V1["min_edge_to_cost_ratio"] == 1.50


def test_apply_baseline_v1_overrides_runner_defaults():
    args = Namespace(target_notional_usdt=1.0, ioc_cross_bps=99.0, min_absolute_residual_bps=1.0)
    apply_baseline_v1(args)
    assert args.target_notional_usdt == 10000.0
    assert args.ioc_cross_bps == 1.0
    assert args.min_absolute_residual_bps == 8.0


def test_anomaly_row_marks_fresh_market_data():
    snap = MicroSpreadSnapshot(
        ready=True, direction=1, edge_bps=120.0, raw_gap_bps=130.0, baseline_gap_bps=10.0,
        binance_move_bps=8.0, mexc_move_bps=1.0, binance_mid=100.0, mexc_mid=98.8,
        age_ms=100.0, binance_age_ms=20.0, mexc_age_ms=100.0, threshold_bps=8.0,
        reason="microspread_confirmed",
    )
    book = LiveBook(
        bid=98.7, ask=98.9, recv_ms=950, exchange_ts_ms=950,
        bids=((98.7, 10.0),), asks=((98.9, 10.0),),
    )
    row = anomaly_row("TEST_USDT", snap, book, 1000)
    assert row["fresh"] is True
    assert row["book_age_ms"] == 50.0
    assert row["residual_bps"] == 120.0


def test_anomaly_row_rejects_stale_book():
    snap = MicroSpreadSnapshot(
        ready=True, direction=1, edge_bps=120.0, raw_gap_bps=130.0, baseline_gap_bps=10.0,
        binance_move_bps=8.0, mexc_move_bps=1.0, binance_mid=100.0, mexc_mid=98.8,
        age_ms=100.0, binance_age_ms=20.0, mexc_age_ms=100.0, threshold_bps=8.0,
        reason="microspread_confirmed",
    )
    book = LiveBook(
        bid=98.7, ask=98.9, recv_ms=0, exchange_ts_ms=0,
        bids=((98.7, 10.0),), asks=((98.9, 10.0),),
    )
    assert anomaly_row("TEST_USDT", snap, book, 1000)["fresh"] is False
