from __future__ import annotations

import asyncio

from rich.console import Console

from . import prelive_liquidation_validation as base
from . import prelive_liquidation_validation_v3 as v3

console = Console()


class FixedInitialMarginBudget:
    """Adapter for legacy margin_fraction plumbing.

    v2 multiplies current balance by args.margin_fraction. This object makes
    that multiplication return min(configured initial margin, current balance),
    so liquidation experiments can vary isolated margin without changing the
    frozen strategy's signal/execution thresholds.
    """

    def __init__(self, initial_margin_usdt: float, starting_balance_usdt: float) -> None:
        self.initial_margin_usdt = float(initial_margin_usdt)
        self.starting_balance_usdt = float(starting_balance_usdt)

    def __rmul__(self, balance: float) -> float:
        return min(max(0.0, float(balance)), self.initial_margin_usdt)

    def __format__(self, spec: str) -> str:
        if spec == ".0%":
            frac = self.initial_margin_usdt / max(self.starting_balance_usdt, 1e-12)
            return format(frac, spec)
        return format(self.initial_margin_usdt, spec)

    def __gt__(self, other) -> bool:
        return self.initial_margin_usdt > float(other)

    def __le__(self, other) -> bool:
        return True


async def run(args) -> None:
    initial_margin = min(float(args.initial_margin_usdt), float(args.balance_usdt))
    console.print(
        "[bold cyan]V4 ISOLATED-MARGIN LIQUIDATION EXPERIMENT[/bold cyan] "
        f"bank=${args.balance_usdt:.2f}; experimental_initial_margin=${initial_margin:.2f}; "
        f"leverage={'MEXC_MAX' if args.leverage <= 0 else str(args.leverage)+'x'}"
    )
    console.print("Initial margin here is an experiment parameter, NOT a frozen baseline-v1 strategy parameter.")
    console.print(
        "Requested notional remains the frozen baseline target. IOC fills only liquidity inside the limit; "
        "the unfilled remainder is cancelled and is NEVER topped up."
    )

    args.margin_fraction = FixedInitialMarginBudget(initial_margin, args.balance_usdt)
    await v3.run(args)


def build_parser():
    p = base.build_parser()
    p.description = (
        "Frozen baseline-v1 LIVE paper strategy with separately configurable isolated-margin liquidation experiment"
    )
    p.add_argument(
        "--initial-margin-usdt",
        type=float,
        default=50.0,
        help="Experimental isolated position margin for liquidation replay; not a baseline-v1 strategy threshold",
    )
    p.add_argument("--max-arrival-spread-bps", type=float, default=20.0)
    p.add_argument("--max-roundtrip-cost-bps", type=float, default=25.0)
    return p


def main() -> None:
    args = build_parser().parse_args()
    if args.balance_usdt <= 0:
        raise SystemExit("--balance-usdt must be > 0")
    if args.initial_margin_usdt <= 0:
        raise SystemExit("--initial-margin-usdt must be > 0")
    if args.max_arrival_spread_bps <= 0 or args.max_roundtrip_cost_bps <= 0:
        raise SystemExit("sanity guard thresholds must be > 0")
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
