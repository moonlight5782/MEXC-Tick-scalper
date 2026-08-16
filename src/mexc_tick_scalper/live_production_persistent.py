from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from rich.console import Console

from .live_production_runner_v2 import build_parser as build_v2_parser, run as run_v2
from .persistent_lag_profile import build_profiles, latest_lifetime_csv, select_profiles

console = Console()


def _resolve_source(csv_arg: str) -> Path:
    return Path(csv_arg) if csv_arg else latest_lifetime_csv(Path.cwd())


def build_parser() -> argparse.ArgumentParser:
    p = build_v2_parser()
    p.description = "Production Binance-lead/MEXC-lag runner restricted to persistent lag pairs"
    p.add_argument("--lifetime-csv", default="")
    p.add_argument("--pair-min-signals", type=int, default=4)
    p.add_argument("--pair-min-median-lifetime-ms", type=float, default=300.0)
    p.add_argument("--pair-min-survival-rate", type=float, default=0.50)
    p.add_argument("--pair-min-strength-ratio", type=float, default=1.50)
    p.add_argument("--strong-min-edge-bps", type=float, default=2.0)
    p.add_argument("--strong-min-leader-advantage-bps", type=float, default=1.0)
    p.add_argument("--strong-edge-to-spread-ratio", type=float, default=1.15)
    return p


async def run(args: argparse.Namespace) -> None:
    source = _resolve_source(args.lifetime_csv)
    profiles = build_profiles(source)
    selected = select_profiles(
        profiles,
        min_signals=args.pair_min_signals,
        min_median_lifetime_ms=args.pair_min_median_lifetime_ms,
        min_survival_rate=args.pair_min_survival_rate,
        min_signal_strength_ratio=args.pair_min_strength_ratio,
    )
    if not selected:
        raise RuntimeError(
            "No pair passed persistent-lag filters. Refusing to trade broad universe; collect/choose a valid lifetime profile."
        )

    selected_symbols = {p.symbol for p in selected}
    requested = {x.strip().upper() for x in args.include_symbols.split(",") if x.strip()}
    if requested:
        selected_symbols &= requested
    if not selected_symbols:
        raise RuntimeError("Persistent profile and --include-symbols have no overlap")

    args.include_symbols = ",".join(sorted(selected_symbols))
    args.min_edge_bps = max(float(args.min_edge_bps), float(args.strong_min_edge_bps))
    args.min_leader_advantage_bps = max(
        float(args.min_leader_advantage_bps), float(args.strong_min_leader_advantage_bps)
    )
    args.edge_to_spread_ratio = max(
        float(args.edge_to_spread_ratio), float(args.strong_edge_to_spread_ratio)
    )

    console.print("[bold red]PRODUCTION PERSISTENT-LAG FILTER[/bold red]")
    console.print(f"Lifetime source: {source.resolve()}")
    for p in selected:
        if p.symbol in selected_symbols:
            console.print(
                f"  ENABLE {p.symbol}: n={p.signals} median={p.median_lifetime_ms:.0f}ms "
                f"p90={p.p90_lifetime_ms:.0f}ms survive={p.survive_execution_rate*100:.1f}% "
                f"strength={p.median_signal_strength_ratio:.2f}x residual={p.median_signal_residual_bps:.2f}bps"
            )
    console.print(
        f"Live event gate: min_edge={args.min_edge_bps:.2f}bps "
        f"min_lead={args.min_leader_advantage_bps:.2f}bps "
        f"spread_ratio={args.edge_to_spread_ratio:.2f}."
    )
    console.print("Broad-universe trading is disabled in this launcher.")

    await run_v2(args)


def main() -> None:
    args = build_parser().parse_args()
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
