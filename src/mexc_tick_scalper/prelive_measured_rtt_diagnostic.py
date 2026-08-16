from __future__ import annotations

import argparse
import asyncio
import statistics
import time
from pathlib import Path

from dotenv import load_dotenv
from rich.console import Console

from .prelive_latency_diagnostic_v2 import build_parser as build_v2_parser, run as run_v2
from .web_execution import MexcWebExecutionAdapter, WebExecutionConfig

console = Console()


def _percentile(values: list[float], q: float) -> float:
    rows = sorted(values)
    if not rows:
        return 0.0
    if len(rows) == 1:
        return rows[0]
    pos = (len(rows) - 1) * q
    lo = int(pos)
    hi = min(lo + 1, len(rows) - 1)
    frac = pos - lo
    return rows[lo] * (1.0 - frac) + rows[hi] * frac


def _load_env() -> None:
    root = Path(__file__).resolve().parents[2]
    load_dotenv(root / ".env", override=False)


async def measure_live_private_rtt(*, samples: int, warmup_samples: int, interval_ms: float) -> list[float]:
    """Measure current LIVE private-web RTT using read-only position reads only."""
    _load_env()
    cfg = WebExecutionConfig.from_env(write_enabled=False)
    measured: list[float] = []
    async with MexcWebExecutionAdapter(cfg) as adapter:
        for _ in range(max(1, warmup_samples)):
            await adapter.get_positions()
            if interval_ms > 0:
                await asyncio.sleep(interval_ms / 1000.0)
        for _ in range(samples):
            started = time.perf_counter_ns()
            await adapter.get_positions()
            measured.append((time.perf_counter_ns() - started) / 1_000_000.0)
            if interval_ms > 0:
                await asyncio.sleep(interval_ms / 1000.0)
    return measured


async def run(args: argparse.Namespace) -> None:
    console.print("[cyan]PRE-LIVE MEASURED-RTT DIAGNOSTIC[/cyan]")
    console.print("Phase 1: measuring current LIVE MEXC private-web RTT with read-only position requests.")
    values = await measure_live_private_rtt(
        samples=args.rtt_samples,
        warmup_samples=args.rtt_warmup_samples,
        interval_ms=args.rtt_interval_ms,
    )
    median = statistics.median(values)
    p90 = _percentile(values, 0.90)
    p95 = _percentile(values, 0.95)
    p99 = _percentile(values, 0.99)
    selected = p95 if args.rtt_profile == "p95" else median
    selected_ms = max(0, int(round(selected)))

    console.print(
        f"Measured RTT: median={median:.1f}ms p90={p90:.1f}ms p95={p95:.1f}ms "
        f"p99={p99:.1f}ms min={min(values):.1f}ms max={max(values):.1f}ms"
    )
    console.print(
        f"Phase 2: V2 shadow will use measured {args.rtt_profile}={selected:.1f}ms "
        f"(rounded to {selected_ms}ms) as its only execution-latency profile."
    )
    console.print(
        "No order endpoint is called. This models the measured private-web request RTT; "
        "matching-engine/fill processing remains unmeasurable without a real order."
    )

    args.latencies_ms = str(selected_ms)
    await run_v2(args)


def build_parser() -> argparse.ArgumentParser:
    p = build_v2_parser()
    p.description = "Read-only V2 lead/lag diagnostic using freshly measured LIVE MEXC private-web RTT"
    p.set_defaults(latencies_ms="")
    p.add_argument("--rtt-samples", type=int, default=40)
    p.add_argument("--rtt-warmup-samples", type=int, default=3)
    p.add_argument("--rtt-interval-ms", type=float, default=100.0)
    p.add_argument("--rtt-profile", choices=("median", "p95"), default="median")
    return p


def main() -> None:
    args = build_parser().parse_args()
    if args.rtt_samples <= 0:
        raise SystemExit("--rtt-samples must be positive")
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
