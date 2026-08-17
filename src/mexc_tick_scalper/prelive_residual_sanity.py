from __future__ import annotations

import argparse
import asyncio
import csv
import time
from pathlib import Path

from rich.console import Console

from .baseline_v1 import BASELINE_V1
from .live_zero_fee_universe import discover_live_zero_fee_crosslisted
from .microspread import MicroSpreadModel
from .microspread_feed import EventBinanceBookTickerFeed, EventMexcDepthFeed
from .persistent_lag_profile import build_profiles, latest_lifetime_csv, select_profiles

console = Console()

FIELDS = [
    "ts_ms", "symbol", "residual_bps", "raw_gap_bps", "baseline_gap_bps",
    "binance_mid", "mexc_mid", "binance_age_ms", "mexc_age_ms", "book_age_ms",
    "mexc_bid", "mexc_ask", "spread_bps", "binance_move_bps", "mexc_move_bps",
    "snapshot_reason", "fresh",
]


def anomaly_row(symbol: str, snap, book, now_ms: int) -> dict[str, object]:
    book_age = float(now_ms - book.recv_ms)
    fresh = (
        snap.binance_age_ms <= BASELINE_V1["max_binance_age_ms"]
        and snap.mexc_age_ms <= BASELINE_V1["max_mexc_age_ms"]
        and book_age <= BASELINE_V1["max_book_age_ms"]
        and snap.binance_age_ms >= 0
        and snap.mexc_age_ms >= 0
        and book_age >= 0
    )
    return {
        "ts_ms": now_ms,
        "symbol": symbol,
        "residual_bps": snap.edge_bps,
        "raw_gap_bps": snap.raw_gap_bps,
        "baseline_gap_bps": snap.baseline_gap_bps,
        "binance_mid": snap.binance_mid,
        "mexc_mid": snap.mexc_mid,
        "binance_age_ms": snap.binance_age_ms,
        "mexc_age_ms": snap.mexc_age_ms,
        "book_age_ms": book_age,
        "mexc_bid": book.bid,
        "mexc_ask": book.ask,
        "spread_bps": book.spread_bps,
        "binance_move_bps": snap.binance_move_bps,
        "mexc_move_bps": snap.mexc_move_bps,
        "snapshot_reason": snap.reason,
        "fresh": fresh,
    }


async def run(args: argparse.Namespace) -> None:
    source = Path(args.lifetime_csv) if args.lifetime_csv else latest_lifetime_csv(Path.cwd())
    profiles = select_profiles(
        build_profiles(source),
        min_signals=BASELINE_V1["pair_min_signals"],
        min_median_lifetime_ms=BASELINE_V1["pair_min_median_lifetime_ms"],
        min_survival_rate=BASELINE_V1["pair_min_survival_rate"],
        min_signal_strength_ratio=BASELINE_V1["pair_min_strength_ratio"],
    )
    keep = {p.symbol for p in profiles}
    contracts = [x for x in await discover_live_zero_fee_crosslisted() if x.mexc_symbol in keep]
    if not contracts:
        raise RuntimeError("No baseline-v1 pair is currently exact-0/0 and Binance-crosslisted")

    models = {
        x.mexc_symbol: MicroSpreadModel(
            horizon_ms=BASELINE_V1["micro_horizon_ms"],
            baseline_seconds=BASELINE_V1["baseline_seconds"],
            baseline_exclusion_ms=BASELINE_V1["baseline_exclusion_ms"],
            min_edge_bps=0.0,
            min_binance_move_bps=0.0,
            max_binance_age_ms=BASELINE_V1["max_binance_age_ms"],
            max_mexc_age_ms=BASELINE_V1["max_mexc_age_ms"],
        ) for x in contracts
    }
    symbols = list(models)
    wake = asyncio.Event()
    binance = EventBinanceBookTickerFeed(contracts, models, wake)
    mexc = EventMexcDepthFeed(symbols, models, wake, depth_limit=BASELINE_V1["depth_limit"])
    await binance.start(); await mexc.start()

    output = Path(args.output_csv or f"residual_sanity_{int(time.time())}.csv")
    seen: dict[str, int] = {}
    deadline = time.monotonic() + args.session_seconds
    console.print(
        f"[bold cyan]LIVE RESIDUAL SANITY[/bold cyan] threshold={args.anomaly_residual_bps:.1f}bps "
        f"output={output}"
    )
    try:
        with output.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=FIELDS)
            writer.writeheader()
            while time.monotonic() < deadline:
                now_ms = int(time.time() * 1000)
                for symbol, model in models.items():
                    book = mexc.books.get(symbol)
                    if book is None:
                        continue
                    snap = model.snapshot(now_ms=now_ms, threshold_bps=0.0)
                    if abs(snap.edge_bps) < args.anomaly_residual_bps:
                        continue
                    if now_ms - seen.get(symbol, 0) < args.cooldown_ms:
                        continue
                    row = anomaly_row(symbol, snap, book, now_ms)
                    writer.writerow(row); fh.flush(); seen[symbol] = now_ms
                    status = "FRESH" if row["fresh"] else "STALE"
                    console.print(
                        f"ANOMALY {symbol} residual={snap.edge_bps:+.2f}bps raw={snap.raw_gap_bps:+.2f} "
                        f"baseline={snap.baseline_gap_bps:+.2f} b_age={snap.binance_age_ms:.0f}ms "
                        f"m_age={snap.mexc_age_ms:.0f}ms book_age={row['book_age_ms']:.0f}ms "
                        f"spread={book.spread_bps:.2f}bps {status}"
                    )
                wake.clear()
                try:
                    await asyncio.wait_for(wake.wait(), timeout=0.02)
                except TimeoutError:
                    pass
    finally:
        await binance.close(); await mexc.close()


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Read-only LIVE sanity monitor for extreme cross-exchange residuals")
    p.add_argument("--session-seconds", type=float, default=86400.0)
    p.add_argument("--anomaly-residual-bps", type=float, default=100.0)
    p.add_argument("--cooldown-ms", type=int, default=250)
    p.add_argument("--output-csv", default="")
    p.add_argument("--lifetime-csv", default="")
    return p


def main() -> None:
    asyncio.run(run(build_parser().parse_args()))


if __name__ == "__main__":
    main()
