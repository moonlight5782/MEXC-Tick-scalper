from mexc_tick_scalper.baseline_v1 import BASELINE_V1
from mexc_tick_scalper.canonical_latency import LatencySnapshot
from mexc_tick_scalper.canonical_shadow import build_parser


def test_current_latency_never_hides_latest_spike_or_inflight_stall():
    snap = LatencySnapshot(
        measured_at=100.0,
        samples=31,
        latest_ms=895.0,
        median_ms=300.0,
        p75_ms=322.0,
        p95_ms=500.0,
        in_flight_ms=0.0,
    )
    assert snap.effective_ms == 895.0

    stalled = LatencySnapshot(
        measured_at=100.0,
        samples=31,
        latest_ms=290.0,
        median_ms=280.0,
        p75_ms=310.0,
        p95_ms=350.0,
        in_flight_ms=740.0,
    )
    assert stalled.effective_ms == 740.0


def test_canonical_parser_does_not_define_fixed_entry_or_exit_latency():
    args = build_parser().parse_args([])
    assert not hasattr(args, "entry_latency_ms")
    assert not hasattr(args, "exit_latency_ms")
    assert args.latency_max_age_ms == 2000.0


def test_frozen_alpha_is_still_the_validated_baseline():
    assert BASELINE_V1["min_absolute_residual_bps"] == 8.0
    assert BASELINE_V1["min_signal_strength_ratio"] == 3.0
    assert BASELINE_V1["min_residual_retention"] == 0.60
    assert BASELINE_V1["min_impulse_retention"] == 0.75
    assert BASELINE_V1["ioc_cross_bps"] == 1.0
    assert BASELINE_V1["trailing_distance_bps"] == 1.5
