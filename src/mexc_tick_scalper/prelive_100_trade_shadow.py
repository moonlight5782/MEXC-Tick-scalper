from __future__ import annotations

import argparse
import asyncio

from rich.console import Console

from . import prelive_persistent_ioc_shadow_v2 as v2
from .baseline_v1 import apply_baseline_v1

console = Console()


class TargetClosedTradesReached(RuntimeError):
    pass


def build_parser() -> argparse.ArgumentParser:
    p = v2.build_parser()
    p.description = "LIVE-data arrival-book IOC paper test using frozen baseline v1 and stopping after an exact number of closed trades"
    p.add_argument("--target-closed-trades", type=int, default=100)
    return p


async def run(args: argparse.Namespace) -> None:
    target = max(1, int(args.target_closed_trades))
    apply_baseline_v1(args)
    original_close = v2._close_trade
    last_stats: v2.Stats | None = None

    def close_and_stop(stats: v2.Stats, pos: v2.Position, now_ms: int) -> None:
        nonlocal last_stats
        original_close(stats, pos, now_ms)
        last_stats = stats
        closed = stats.wins + stats.losses + stats.flats
        if closed >= target:
            raise TargetClosedTradesReached

    v2._close_trade = close_and_stop
    try:
        console.print(
            f"[bold cyan]EXACT {target}-CLOSED-TRADE LIVE PAPER TEST / FROZEN BASELINE V1[/bold cyan] - NO REAL ORDERS"
        )
        console.print(
            "Trading parameters are forcibly loaded from baseline_v1.py; only test duration, signal ceiling, "
            "lifetime CSV path and closed-trade stop condition remain run controls."
        )
        await v2.run(args)
    except TargetClosedTradesReached:
        if last_stats is not None:
            console.print(f"\n[bold]FINAL EXACT {target}-TRADE BASELINE V1 REPORT[/bold]")
            console.print(v2._summary(last_stats))
    finally:
        v2._close_trade = original_close


def main() -> None:
    args = build_parser().parse_args()
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
