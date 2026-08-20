from __future__ import annotations

import asyncio

from . import auto_discovery_shadow_v2 as liq


base = liq.base
_ORIGINAL_TRAILING = base.runner.PositiveTrailing
_ORIGINAL_LIQ_RECORD_CLOSE = liq._ORIGINAL_AUTO_RECORD_CLOSE
_ACTIVE_ARGS = None
_ORIGINAL_CONVERGENCE_BPS = None
_ORIGINAL_CONVERGENCE_FRACTION = None
_PROFIT_RUNNER_ARMED = False


def _restore_convergence() -> None:
    global _PROFIT_RUNNER_ARMED
    if _ACTIVE_ARGS is not None:
        if _ORIGINAL_CONVERGENCE_BPS is not None:
            _ACTIVE_ARGS.convergence_bps = _ORIGINAL_CONVERGENCE_BPS
        if _ORIGINAL_CONVERGENCE_FRACTION is not None:
            _ACTIVE_ARGS.convergence_fraction = _ORIGINAL_CONVERGENCE_FRACTION
    _PROFIT_RUNNER_ARMED = False


class ProfitRunnerTrailing(_ORIGINAL_TRAILING):
    """Keep frozen trailing logic, but let a confirmed winner outrun convergence.

    Before the executable position reaches ``profit_runner_arm_bps`` the frozen
    lead-lag exits are unchanged. Once that profit has actually been observable
    on executable MEXC depth, convergence is disabled only for the current
    position. Emergency adverse/reversal/leader-retrace, trailing, timeout and
    the separate fair-price liquidation guard remain active.
    """

    def update(self, move_bps: float):
        global _PROFIT_RUNNER_ARMED
        stop = super().update(move_bps)
        args = _ACTIVE_ARGS
        if args is not None and not _PROFIT_RUNNER_ARMED:
            arm_bps = max(0.0, float(getattr(args, "profit_runner_arm_bps", 5.0)))
            if self.peak_bps + 1e-9 >= arm_bps:
                _PROFIT_RUNNER_ARMED = True
                # Core computes conv=max(convergence_bps,
                # abs(entry_residual)*convergence_fraction). Negative values make
                # the non-negative abs(residual) <= conv condition impossible.
                args.convergence_bps = -1.0
                args.convergence_fraction = -1.0
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
    global _ACTIVE_ARGS, _ORIGINAL_CONVERGENCE_BPS, _ORIGINAL_CONVERGENCE_FRACTION
    _ACTIVE_ARGS = args
    _ORIGINAL_CONVERGENCE_BPS = float(args.convergence_bps)
    _ORIGINAL_CONVERGENCE_FRACTION = float(args.convergence_fraction)
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
        _ORIGINAL_CONVERGENCE_BPS = None
        _ORIGINAL_CONVERGENCE_FRACTION = None


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
