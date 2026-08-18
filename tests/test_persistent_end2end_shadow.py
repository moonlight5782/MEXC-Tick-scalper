from argparse import Namespace

from mexc_tick_scalper.baseline_v1 import BASELINE_V1, apply_baseline_v1
from mexc_tick_scalper.persistent_end2end_shadow import (
    LatencySample,
    _latency_profile,
    _load_latency_samples,
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


def test_fallback_latency_is_explicit_and_coherent():
    args = Namespace(latency_csv="", entry_latency_ms=650.0, exit_latency_ms=350.0)
    assert _latency_profile(args) == [LatencySample(650.0, 350.0)]


def test_invalid_latency_does_not_silently_become_zero():
    args = Namespace(latency_csv="", entry_latency_ms=0.0, exit_latency_ms=350.0)
    try:
        _latency_profile(args)
    except ValueError as exc:
        assert "positive" in str(exc)
    else:
        raise AssertionError("zero entry latency must be rejected")
