from types import SimpleNamespace
import math

import mexc_tick_scalper.auto_discovery_testnet_xrp_profit_hold as ph


def test_profit_hold_changes_only_after_first_positive_tick() -> None:
    args = SimpleNamespace(
        mid_adverse_cut_bps=0.01,
        leader_retrace_exit_bps=1.5,
        reversal_edge_bps=0.75,
        no_progress_ms=3000,
        max_hold_ms=15000,
        profit_runner_arm_bps=5.0,
        min_absolute_residual_bps=8.0,
        min_signal_strength_ratio=3.0,
    )
    old_args = ph._ACTIVE_ARGS
    old_policy = ph._SAVED_PRE_PROFIT_POLICY
    try:
        ph._ACTIVE_ARGS = args
        ph._SAVED_PRE_PROFIT_POLICY = {}
        trail = ph.ProfitHoldTrailing(distance_bps=1.5)

        # Losing/flat position keeps the exact original policy.
        assert trail.update(-1.0) is None
        assert args.mid_adverse_cut_bps == 0.01
        assert args.leader_retrace_exit_bps == 1.5
        assert args.reversal_edge_bps == 0.75
        assert args.no_progress_ms == 3000
        assert args.max_hold_ms == 15000
        assert args.min_absolute_residual_bps == 8.0
        assert args.min_signal_strength_ratio == 3.0

        # First positive executable tick arms winner hold and a positive floor.
        stop = trail.update(0.20)
        assert stop is not None and 0.0 < stop <= 0.20
        assert math.isinf(args.mid_adverse_cut_bps)
        assert math.isinf(args.leader_retrace_exit_bps)
        assert math.isinf(args.reversal_edge_bps)
        assert args.no_progress_ms > 1_000_000
        assert args.max_hold_ms > 1_000_000

        # Entry thresholds were never touched.
        assert args.min_absolute_residual_bps == 8.0
        assert args.min_signal_strength_ratio == 3.0
    finally:
        ph._ACTIVE_ARGS = old_args
        ph._SAVED_PRE_PROFIT_POLICY = old_policy
