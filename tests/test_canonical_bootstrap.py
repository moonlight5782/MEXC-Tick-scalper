from mexc_tick_scalper import prelive_lag_lifetime_diagnostic as diagnostic
from mexc_tick_scalper.baseline_v1 import BASELINE_V1
from mexc_tick_scalper.canonical_bootstrap import _apply_shared_baseline


def test_bootstrap_uses_baseline_values_for_shared_knobs():
    args = diagnostic.build_parser().parse_args([])
    _apply_shared_baseline(args)
    for key in (
        "micro_horizon_ms",
        "baseline_seconds",
        "baseline_exclusion_ms",
        "noise_window_ms",
        "residual_noise_multiplier",
        "binance_noise_multiplier",
        "min_edge_bps",
        "min_net_edge_bps",
        "edge_to_spread_ratio",
        "min_binance_move_bps",
        "min_leader_advantage_bps",
        "min_lead_ratio",
        "confirm_updates",
        "confirm_ms",
        "rearm_fraction",
        "max_binance_age_ms",
        "max_mexc_age_ms",
        "max_book_age_ms",
        "warmup_seconds",
        "depth_limit",
        "convergence_bps",
        "convergence_fraction",
        "reversal_edge_bps",
    ):
        assert getattr(args, key) == BASELINE_V1[key]
