from __future__ import annotations

import argparse
import asyncio
import time
from pathlib import Path

from rich.console import Console

from .baseline_v1 import BASELINE_V1
from .canonical_env import load_project_env
from . import prelive_lag_lifetime_diagnostic as diagnostic
from .persistent_lag_profile import build_profiles, select_profiles

console = Console()


def _apply_shared_baseline(args: argparse.Namespace) -> argparse.Namespace:
    """Use BASELINE_V1 values for every lifetime-diagnostic knob with the same meaning."""
    for name, value in BASELINE_V1.items():
        if hasattr(args, name):
            setattr(args, name, value)
    return args


def _eligible(path: Path) -> list[str]:
    rows = select_profiles(
        build_profiles(path),
        min_signals=int(BASELINE_V1["pair_min_signals"]),
        min_median_lifetime_ms=float(BASELINE_V1["pair_min_median_lifetime_ms"]),
        min_survival_rate=float(BASELINE_V1["pair_min_survival_rate"]),
        min_signal_strength_ratio=float(BASELINE_V1["pair_min_strength_ratio"]),
    )
    return [row.symbol for row in rows]


async def bootstrap(*, session_seconds: float, max_signals: int, output: Path) -> Path:
    parser = diagnostic.build_parser()
    args = parser.parse_args([])
    _apply_shared_baseline(args)
    args.session_seconds = float(session_seconds)
    args.max_signals = int(max_signals)
    args.csv = str(output)
    console.print(
        "[bold cyan]CANONICAL PROFILE BOOTSTRAP[/bold cyan] - read-only LIVE Binance/MEXC; no order writes"
    )
    console.print(
        f"Collecting up to {args.max_signals} lag episodes for <= {args.session_seconds:.0f}s using BASELINE_V1-compatible settings."
    )
    await diagnostic.run(args)
    if not output.exists():
        raise RuntimeError(f"profile bootstrap did not create {output}")
    eligible = _eligible(output)
    if not eligible:
        raise RuntimeError(
            "profile bootstrap completed but produced no BASELINE_V1-eligible persistent-lag symbols; "
            "do not trade by weakening thresholds—collect a longer sample instead"
        )
    console.print("[green]CANONICAL PROFILE READY[/green] " + ",".join(eligible))
    return output


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Create a current persistent-lag profile for the canonical runner")
    p.add_argument("--session-seconds", type=float, default=900.0)
    p.add_argument("--max-signals", type=int, default=120)
    p.add_argument("--output", default="")
    return p


def main() -> None:
    load_project_env()
    args = build_parser().parse_args()
    if args.session_seconds <= 0 or args.max_signals <= 0:
        raise SystemExit("session-seconds and max-signals must be positive")
    output = Path(args.output or f"prelive_lag_lifetime_canonical_{int(time.time())}.csv")
    asyncio.run(bootstrap(session_seconds=args.session_seconds, max_signals=args.max_signals, output=output))


if __name__ == "__main__":
    main()
