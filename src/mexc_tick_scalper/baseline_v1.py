from __future__ import annotations

# Frozen after the first 100-closed-trade arrival-book IOC validation run.
# Do not edit these values in-place. Create baseline_v2.py for future strategy changes.
BASELINE_V1 = {
    "target_notional_usdt": 10000.0,
    "pair_min_signals": 4,
    "pair_min_median_lifetime_ms": 300.0,
    "pair_min_survival_rate": 0.50,
    "pair_min_strength_ratio": 1.50,
    "micro_horizon_ms": 100,
    "baseline_seconds": 8.0,
    "baseline_exclusion_ms": 1000,
    "noise_window_ms": 8000,
    "residual_noise_multiplier": 3.0,
    "binance_noise_multiplier": 1.5,
    "min_edge_bps": 2.0,
    "min_net_edge_bps": 0.5,
    "edge_to_spread_ratio": 1.2,
    "min_binance_move_bps": 1.0,
    "min_leader_advantage_bps": 1.0,
    "min_lead_ratio": 1.35,
    "confirm_updates": 2,
    "confirm_ms": 15,
    "rearm_fraction": 0.35,
    "min_signal_strength_ratio": 3.0,
    "min_absolute_residual_bps": 8.0,
    "min_residual_retention": 0.60,
    "min_impulse_retention": 0.75,
    "min_edge_after_spread_bps": 2.0,
    "ioc_cross_bps": 1.0,
    "max_entry_slippage_bps": 1.0,
    "min_filled_notional_usdt": 50.0,
    "min_executable_net_edge_bps": 2.0,
    "min_edge_to_cost_ratio": 1.50,
    "max_binance_age_ms": 300.0,
    "max_mexc_age_ms": 2000.0,
    "max_book_age_ms": 750.0,
    "warmup_seconds": 10.0,
    "depth_limit": 20,
    "min_hold_ms": 50,
    "max_hold_ms": 15000,
    "no_progress_ms": 3000,
    "min_progress_bps": 0.5,
    "convergence_bps": 0.25,
    "convergence_fraction": 0.25,
    "min_catchup_bps": 1.0,
    "leader_retrace_exit_bps": 1.5,
    "reversal_edge_bps": 0.75,
    "mid_adverse_cut_bps": 3.0,
    "trailing_distance_bps": 1.5,
    "rtt_samples": 40,
    "rtt_warmup_samples": 3,
    "rtt_interval_ms": 100.0,
}


def apply_baseline_v1(args):
    for name, value in BASELINE_V1.items():
        setattr(args, name, value)
    return args
