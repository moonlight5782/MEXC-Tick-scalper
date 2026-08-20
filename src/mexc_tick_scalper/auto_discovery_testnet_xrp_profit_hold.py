from __future__ import annotations

import asyncio
import math

from . import auto_discovery_testnet_xrp_fixed as fixed

# IMPORTANT: this wrapper changes ONLY management of an already-open profitable
# position. Entry thresholds, signal gate, arrival economics, sizing and Demo
# execution remain exactly those of auto_discovery_testnet_xrp_fixed (baseline 8/3).

_ORIGINAL_TRAILING = fixed.PositiveTrailing
_ACTIVE_ARGS = None
_ORIGINAL_POLICY: dict[str, float | int] = {}
PROFIT_LOCK_FLOOR_BPS = 0.10


def _restore_policy() -> None:
    if _ACTIVE_ARGS is None or not _ORIGINAL_POLICY:
        return
    _ACTIVE_ARGS.mid_adverse_cut_bps = _ORIGINAL_POLICY["mid_adverse_cut_bps"]
    _ACTIVE_ARGS.leader_retrace_exit_bps = _ORIGINAL_POLICY["leader_retrace_exit_bps"]
    _ACTIVE_ARGS.reversal_edge_bps = _ORIGINAL_POLICY["reversal_edge_bps"]
    _ACTIVE_ARGS.no_progress_ms = _ORIGINAL_POLICY["no_progress_ms"]
    _ACTIVE_ARGS.max_hold_ms = _ORIGINAL_POLICY["max_hold_ms"]
    _ACTIVE_ARGS.profit_runner_arm_bps = _ORIGINAL_POLICY["profit_runner_arm_bps"]


def _arm_profit_hold() -> None:
    if _ACTIVE_ARGS is None:
        return
    # Once the actual Demo position is profitable, lead-lag thesis exits must not
    # cut the winner. Positive trailing owns the market-price exit from here.
    _ACTIVE_ARGS.mid_adverse_cut_bps = math.inf
    _ACTIVE_ARGS.leader_retrace_exit_bps = math.inf
    _ACTIVE_ARGS.reversal_edge_bps = math.inf
    _ACTIVE_ARGS.no_progress_ms = 2_147_483_647
    _ACTIVE_ARGS.max_hold_ms = 2_147_483_647


class ProfitHoldTrailing:
    """Original trailing plus permanent profit-hold after first positive PnL."""

    def __init__(self, distance_bps: float) -> None:
        # A fresh position must start with all original pre-profit protections.
        _restore_policy()
        self._inner = _ORIGINAL_TRAILING(distance_bps=distance_bps)
        self._armed = False

    @property
    def distance_bps(self) -> float:
        return self._inner.distance_bps

    @property
    def peak_bps(self) -> float:
        return self._inner.peak_bps

    @property
    def stop_bps(self) -> float | None:
        return self._inner.stop_bps

    @stop_bps.setter
    def stop_bps(self, value: float | None) -> None:
        self._inner.stop_bps = value

    def update(self, move_bps: float) -> float | None:
        stop = self._inner.update(move_bps)
        if move_bps > 0.0 and not self._armed:
            self._armed = True
            _arm_profit_hold()
            # Lock a small but strictly positive gross profit immediately, without
            # placing the stop above the currently observable executable profit.
            floor = min(PROFIT_LOCK_FLOOR_BPS, move_bps * 0.5)
            if floor > 0.0:
                self._inner.stop_bps = floor if self._inner.stop_bps is None else max(self._inner.stop_bps, floor)
            stop = self._inner.stop_bps
            fixed.console.print(
                f"[bold green]PROFIT HOLD ARMED[/bold green] {fixed.SYMBOL} "
                f"executable={move_bps:+.2f}bps stop={stop:+.2f}bps; "
                "lead-lag exits disabled for this winner; positive trailing owns exit"
            )
        return self._inner.stop_bps if self._armed else stop


async def run(args) -> None:
    global _ACTIVE_ARGS, _ORIGINAL_POLICY

    # Do not touch entry parameters. Baseline apply_baseline_v1() in fixed.main()
    # keeps the original min_absolute_residual_bps=8 and strength=3x.
    _ACTIVE_ARGS = args
    _ORIGINAL_POLICY = {
        "mid_adverse_cut_bps": args.mid_adverse_cut_bps,
        "leader_retrace_exit_bps": args.leader_retrace_exit_bps,
        "reversal_edge_bps": args.reversal_edge_bps,
        "no_progress_ms": args.no_progress_ms,
        "max_hold_ms": args.max_hold_ms,
        "profit_runner_arm_bps": args.profit_runner_arm_bps,
    }

    # fixed.run() applies its immediate-exit policy after entering run(), so our
    # saved values are refreshed below by ProfitHoldTrailing construction. Arm the
    # fixed runner flag on the first positive executable tick so convergence is
    # suppressed on the same tick as profit-hold.
    args.profit_runner_arm_bps = 1e-9

    original_trailing = fixed.PositiveTrailing
    fixed.PositiveTrailing = ProfitHoldTrailing
    try:
        fixed.console.print(
            "[bold cyan]XRP TESTNET: ORIGINAL ENTRY + PROFIT HOLD[/bold cyan] "
            "entry=baseline 8bps/3x; only profitable-position management is changed"
        )
        await fixed.run(args)
    finally:
        _restore_policy()
        fixed.PositiveTrailing = original_trailing
        _ACTIVE_ARGS = None
        _ORIGINAL_POLICY = {}


def main() -> None:
    args = fixed.build_parser().parse_args()
    fixed.auto.apply_baseline_v1(args)
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        raise SystemExit(130)
    except Exception as exc:
        fixed.console.print(f"[red]XRP PROFIT-HOLD TESTNET STOPPED:[/red] {type(exc).__name__}: {exc}")
        raise SystemExit(2)


if __name__ == "__main__":
    main()
