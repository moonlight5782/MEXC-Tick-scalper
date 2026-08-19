from argparse import Namespace

from mexc_tick_scalper.baseline_v1 import BASELINE_V1, apply_baseline_v1
from mexc_tick_scalper.persistent_end2end_shadow import (
    LatencyProvider,
    LatencySample,
    PendingEntry,
    _load_latency_samples,
    _must_drain,
    _run_budget_open,
    build_parser,
)


def test_frozen_alpha_is_still_forced():
    args = Namespace(
        target_notional_usdt=1.0,
        min_absolute_residual_bps=1.0,
        min_signal_strength_ratio=1.0,
        ioc_cross_bps=99.0,
    )
    apply_baseline_v1(args)
    assert args.target_notional_usdt == BASELINE_V1["target_notional_usdt"] == 10000.0
    assert args.min_absolute_residual_bps == BASELINE_V1["min_absolute_residual_bps"] == 8.0
    assert args.min_signal_strength_ratio == BASELINE_V1["min_signal_strength_ratio"] == 3.0
    assert args.ioc_cross_bps == BASELINE_V1["ioc_cross_bps"] == 1.0


def test_loads_shadow_entry_and_exit_from_same_row(tmp_path):
    path = tmp_path / "latency.csv"
    path.write_text(
        "signal_to_fill_ms,exit_decision_to_fill_ms\n"
        "173,339\n"
        "194,637\n",
        encoding="utf-8",
    )
    assert _load_latency_samples(path) == [LatencySample(173.0, 339.0), LatencySample(194.0, 637.0)]


def test_loads_demo_provisional_and_exit_proxy_from_same_row(tmp_path):
    path = tmp_path / "demo.csv"
    path.write_text(
        "signal_to_provisional_ms,ioc_post_roundtrip_ms\n"
        "682.365,363.239\n",
        encoding="utf-8",
    )
    assert _load_latency_samples(path) == [LatencySample(682.365, 363.239)]


def test_loads_older_demo_entry_as_build_plus_confirmation(tmp_path):
    path = tmp_path / "old_demo.csv"
    path.write_text(
        "signal_to_ioc_post_ms,ioc_confirmation_ms,ioc_post_roundtrip_ms\n"
        "20,640,350\n",
        encoding="utf-8",
    )
    assert _load_latency_samples(path) == [LatencySample(660.0, 350.0)]


def test_realtime_is_default_and_no_fixed_latency_cli_exists():
    parser = build_parser()
    args = parser.parse_args([])
    assert args.latency_csv == ""
    assert args.latency_profile == "p75"
    assert not hasattr(args, "entry_latency_ms")
    assert not hasattr(args, "exit_latency_ms")


def test_latency_provider_replay_is_only_explicit_non_realtime_mode(tmp_path):
    path = tmp_path / "latency.csv"
    path.write_text(
        "signal_to_fill_ms,exit_decision_to_fill_ms\n173,339\n",
        encoding="utf-8",
    )
    args = Namespace(
        latency_profile="p75",
        latency_max_age_seconds=2.0,
        latency_csv=str(path),
        latency_probe_interval_ms=250.0,
        latency_window=31,
        latency_min_samples=5,
    )
    provider = LatencyProvider(args)
    assert provider.mode == "REPLAY:1"
    entry = provider.entry()
    assert entry is not None
    assert entry.value_ms == 173.0
    assert entry.replay_exit_ms == 339.0
    exit_ = provider.exit(entry.replay_exit_ms)
    assert exit_ is not None
    assert exit_.value_ms == 339.0


def test_session_limits_stop_new_signals_but_do_not_define_terminal_state():
    args = Namespace(max_signals=10, target_closed_trades=100)
    stats = Namespace(signals=10)
    assert not _run_budget_open(50.0, 100.0, stats, args, [])
    # A pending/open lifecycle is still considered work that must be drained.
    assert _must_drain(object(), None)
    assert _must_drain(None, object())
    assert not _must_drain(None, None)


def test_deadline_stops_new_signals_even_before_trade_target():
    args = Namespace(max_signals=1000, target_closed_trades=100)
    stats = Namespace(signals=1)
    assert not _run_budget_open(101.0, 100.0, stats, args, [])
