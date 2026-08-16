from __future__ import annotations

import argparse
import asyncio
import statistics
import time
from pathlib import Path

from dotenv import load_dotenv
from rich.console import Console

from .web_execution import MexcWebExecutionAdapter, WebExecutionConfig

console = Console()


def _load_env() -> None:
    root = Path(__file__).resolve().parents[2]
    load_dotenv(root / ".env", override=False)


def _percentile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    rows = sorted(values)
    if len(rows) == 1:
        return rows[0]
    pos = (len(rows) - 1) * q
    lo = int(pos)
    hi = min(lo + 1, len(rows) - 1)
    frac = pos - lo
    return rows[lo] * (1.0 - frac) + rows[hi] * frac


async def run(args: argparse.Namespace) -> None:
    _load_env()
    cfg = WebExecutionConfig.from_env(write_enabled=False)
    samples: list[float] = []

    console.print("[cyan]LIVE MEXC READ-ONLY PRIVATE RTT PROBE[/cyan]")
    console.print("Uses the existing web-session token and GET/read-only position requests only.")
    console.print("No order endpoint and no LIVE write request is used.")

    async with MexcWebExecutionAdapter(cfg) as adapter:
        # Warm the DNS/TLS/HTTP connection before collecting measurements.
        for _ in range(max(1, args.warmup_samples)):
            await adapter.get_positions()
            if args.interval_ms > 0:
                await asyncio.sleep(args.interval_ms / 1000.0)

        for i in range(1, args.samples + 1):
            started = time.perf_counter_ns()
            await adapter.get_positions()
            elapsed_ms = (time.perf_counter_ns() - started) / 1_000_000.0
            samples.append(elapsed_ms)

            if i == 1 or i % args.report_every == 0 or i == args.samples:
                median = statistics.median(samples)
                p90 = _percentile(samples, 0.90)
                p95 = _percentile(samples, 0.95)
                p99 = _percentile(samples, 0.99)
                console.print(
                    f"samples={i}/{args.samples} last={elapsed_ms:.1f}ms "
                    f"median={median:.1f}ms p90={p90:.1f}ms p95={p95:.1f}ms p99={p99:.1f}ms "
                    f"min={min(samples):.1f}ms max={max(samples):.1f}ms"
                )

            if args.interval_ms > 0 and i < args.samples:
                await asyncio.sleep(args.interval_ms / 1000.0)

    console.print("\n[bold]FINAL LIVE READ-ONLY RTT[/bold]")
    console.print(
        f"samples={len(samples)} median={statistics.median(samples):.1f}ms "
        f"p90={_percentile(samples, 0.90):.1f}ms p95={_percentile(samples, 0.95):.1f}ms "
        f"p99={_percentile(samples, 0.99):.1f}ms min={min(samples):.1f}ms max={max(samples):.1f}ms"
    )
    console.print(
        "This is the measured LIVE private-web request RTT. It does not include the exchange's internal "
        "order matching/fill time, which cannot be measured without submitting an order."
    )


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Measure MEXC LIVE web-session private read-only RTT")
    p.add_argument("--samples", type=int, default=100)
    p.add_argument("--warmup-samples", type=int, default=3)
    p.add_argument("--interval-ms", type=float, default=100.0)
    p.add_argument("--report-every", type=int, default=10)
    return p


def main() -> None:
    args = build_parser().parse_args()
    if args.samples <= 0:
        raise SystemExit("--samples must be positive")
    if args.report_every <= 0:
        raise SystemExit("--report-every must be positive")
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
