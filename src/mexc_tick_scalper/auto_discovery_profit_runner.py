from __future__ import annotations

import asyncio
import math

from . import auto_discovery_shadow_v2 as liq


base = liq.base
_ORIGINAL_TRAILING = base.runner.PositiveTrailing
_ORIGINAL_LIQ_RECORD_CLOSE = liq._ORIGINAL_AUTO_RECORD_CLOSE
_ACTIVE_ARGS = None
_ORIGINAL_MIN_CATCHUP_BPS = None
_PROFIT_RUNNER_ARMED = False


def _restore_convergence() -> None:
    global _PROFIT_RUNNER_ARMED
    if _ACTIVE_ARGS is not None and _ORIGINAL_MIN_CATCHUP_BPS is not None:
        _ACTIVE_ARGS.min_catchup_bps = _ORIGINAL_MIN_CATCHUP_BPS
    _PROFIT_RUNNER_ARMED = False


class ProfitRunnerTrailing(_ORIGINAL_TRAILING):
    """Let a confirmed executable winner outrun the convergence take-profit.

    Before ``profit_runner_arm_bps`` all frozen exits remain unchanged. Once the
    executable peak reaches the threshold, only the convergence take-profit is
    suppressed for the current position. Emergency adverse, leader retrace,
    residual reversal, trailing, timeout/no-progress and the separate LIVE MEXC
    fair-price liquidation guard remain active.

    Core computes its local convergence threshold before calling ``update()``,
    but reads ``args.min_catchup_bps`` afterwards when it evaluates the actual
    convergence branch.  Setting min_catchup_bps to +inf therefore suppresses
    convergence in the SAME market iteration in which the runner arms, avoiding
    the previous same-tick race where an armed winner could still close by
    ``mexc_catchup_convergence``.
    """

    def update(self, move_bps: float):
        global _PROFIT_RUNNER_ARMED
        stop = super().update(move_bps)
        args = _ACTIVE_ARGS
        if args is not None and not _PROFIT_RUNNER_ARMED:
            arm_bps = max(0.0, float(getattr(args, "profit_runner_arm_bps", 5.0)))
            if self.peak_bps + 1e-9 >= arm_bps:
                _PROFIT_RUNNER_ARMED = True
                args.min_catchup_bps = math.inf
                base.console.print(
                    f"[bold green]PROFIT RUNNER ARMED[/bold green] peak={self.peak_bps:.2f}bps "
                    f"threshold={arm_bps:.2f}bps; convergence exit disabled for this position; "
                    f"trailing/reversal/emergency/liquidation protection stay active."
                )
        return stop


def _profit_runner_record_close(stats, row) -> None:
    try:
        _ORIGINAL_LIQ_RECORD_CLOSE(stats, row)
    finally:
        _restore_convergence()


async def run(args):
    global _ACTIVE_ARGS, _ORIGINAL_MIN_CATCHUP_BPS
    _ACTIVE_ARGS = args
    _ORIGINAL_MIN_CATCHUP_BPS = float(args.min_catchup_bps)
    _restore_convergence()

    original_trailing = base.runner.PositiveTrailing
    original_liq_record = liq._ORIGINAL_AUTO_RECORD_CLOSE
    base.runner.PositiveTrailing = ProfitRunnerTrailing
    liq._ORIGINAL_AUTO_RECORD_CLOSE = _profit_runner_record_close
    try:
        base.console.print(
            f"[bold cyan]PROFIT RUNNER[/bold cyan] arm at +{float(args.profit_runner_arm_bps):.2f}bps executable peak; "
            "before arm all frozen exits remain unchanged; after arm convergence no longer takes profit early."
        )
        return await liq.run(args)
    finally:
        _restore_convergence()
        base.runner.PositiveTrailing = original_trailing
        liq._ORIGINAL_AUTO_RECORD_CLOSE = original_liq_record
        _ACTIVE_ARGS = None
        _ORIGINAL_MIN_CATCHUP_BPS = None


def build_parser():
    p = base.build_parser()
    p.description = "AUTO discovery + fair-price liquidation guard + winner profit-runner shadow"
    p.add_argument(
        "--profit-runner-arm-bps",
        type=float,
        default=5.0,
        help="after executable peak reaches this profit, ignore convergence and let trailing manage the winner",
    )
    return p


def main() -> None:
    args = build_parser().parse_args()
    base.apply_baseline_v1(args)
    if args.discovery_top <= 0:
        raise SystemExit("--discovery-top must be positive")
    if args.profit_runner_arm_bps < 0:
        raise SystemExit("--profit-runner-arm-bps must be non-negative")
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
