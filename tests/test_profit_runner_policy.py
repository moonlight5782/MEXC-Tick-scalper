from types import SimpleNamespace
import math

import mexc_tick_scalper.auto_discovery_profit_runner as pr


def test_profit_runner_arms_after_executable_peak_and_restores_convergence() -> None:
    args = SimpleNamespace(
        profit_runner_arm_bps=5.0,
        min_catchup_bps=1.0,
    )
    pr._ACTIVE_ARGS = args
    pr._ORIGINAL_MIN_CATCHUP_BPS = args.min_catchup_bps
    pr._PROFIT_RUNNER_ARMED = False
    try:
        trail = pr.ProfitRunnerTrailing(distance_bps=1.0)
        trail.update(4.9)
        assert not pr._PROFIT_RUNNER_ARMED
        assert args.min_catchup_bps == 1.0

        trail.update(5.0)
        assert pr._PROFIT_RUNNER_ARMED
        assert math.isinf(args.min_catchup_bps)
        assert trail.stop_bps == 2.0

        # Core evaluates the convergence branch after trailing.update(). With
        # min_catchup_bps=+inf, same-tick convergence cannot pass.
        mid_move_bps = 12.0
        assert not (mid_move_bps >= args.min_catchup_bps)

        pr._restore_convergence()
        assert not pr._PROFIT_RUNNER_ARMED
        assert args.min_catchup_bps == 1.0
    finally:
        pr._ACTIVE_ARGS = None
        pr._ORIGINAL_MIN_CATCHUP_BPS = None
        pr._PROFIT_RUNNER_ARMED = False


def test_profit_runner_keeps_original_trailing_ratchet() -> None:
    trail = pr.ProfitRunnerTrailing(distance_bps=1.5)
    old_args = pr._ACTIVE_ARGS
    pr._ACTIVE_ARGS = None
    try:
        assert trail.update(3.0) == 0.5
        assert trail.update(5.0) == 2.0
        assert trail.update(10.0) == 8.5
        assert trail.update(9.0) == 8.5
    finally:
        pr._ACTIVE_ARGS = old_args
