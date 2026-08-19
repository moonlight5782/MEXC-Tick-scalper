from __future__ import annotations

import argparse
import asyncio
import time

from rich.console import Console

from . import prelive_persistent_ioc_shadow_v2 as v2
from .baseline_v1 import apply_baseline_v1
from .lead_lag_strategy import LagDecision, LeadLagGate
from .live_production_runner import FeeCache, _fee_loop
from .live_zero_fee_universe import discover_live_zero_fee_crosslisted
from .microspread import MicroSpreadModel
from .microspread_feed import EventBinanceBookTickerFeed, EventMexcDepthFeed


console = Console()
SYMBOL = "BTW_USDT"


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="LIVE BTW_USDT frozen-entry diagnostic; NO ORDERS")
    p.add_argument("--interval", type=float, default=1.0)
    return p


async def run(args: argparse.Namespace) -> None:
    frozen = v2.build_parser().parse_args([])
    apply_baseline_v1(frozen)

    contracts = [x for x in await discover_live_zero_fee_crosslisted() if x.mexc_symbol == SYMBOL]
    if not contracts:
        raise RuntimeError(f"{SYMBOL} is not currently LIVE MEXC/Binance crosslisted exact-0/0")
    contract = contracts[0]

    model = MicroSpreadModel(
        horizon_ms=frozen.micro_horizon_ms,
        baseline_seconds=frozen.baseline_seconds,
        baseline_exclusion_ms=frozen.baseline_exclusion_ms,
        min_edge_bps=0.0,
        min_binance_move_bps=0.0,
        max_binance_age_ms=frozen.max_binance_age_ms,
        max_mexc_age_ms=frozen.max_mexc_age_ms,
    )
    models = {SYMBOL: model}
    gate = LeadLagGate(
        noise_window_ms=frozen.noise_window_ms,
        residual_noise_multiplier=frozen.residual_noise_multiplier,
        binance_noise_multiplier=frozen.binance_noise_multiplier,
        min_edge_bps=frozen.min_edge_bps,
        min_net_edge_bps=frozen.min_net_edge_bps,
        spread_ratio=frozen.edge_to_spread_ratio,
        min_binance_move_bps=frozen.min_binance_move_bps,
        min_leader_advantage_bps=frozen.min_leader_advantage_bps,
        min_lead_ratio=frozen.min_lead_ratio,
        confirm_updates=frozen.confirm_updates,
        confirm_ms=frozen.confirm_ms,
        rearm_fraction=frozen.rearm_fraction,
    )

    wake = asyncio.Event()
    binance = EventBinanceBookTickerFeed([contract], models, wake)
    mexc = EventMexcDepthFeed([SYMBOL], models, wake, depth_limit=frozen.depth_limit)
    fees = FeeCache()
    fee_stop = asyncio.Event()

    await binance.start()
    await mexc.start()
    fee_task = asyncio.create_task(_fee_loop(fees, fee_stop))

    console.print("[bold cyan]BTW LIVE WATCH[/bold cyan] - LIVE Binance + LIVE MEXC, NO ORDERS")
    console.print(
        f"Frozen entry gates: residual>={frozen.min_absolute_residual_bps:.1f}bps "
        f"strength>={frozen.min_signal_strength_ratio:.1f}x spread/cost + LeadLagGate"
    )
    console.print("Gate observation is event-driven exactly like the strategy; display is rate-limited only.")

    next_print = 0.0
    latest_decision: LagDecision | None = None
    latest_status = "warming"
    latest_book_age = 0
    latest_spread = 0.0

    try:
        while True:
            try:
                await asyncio.wait_for(wake.wait(), timeout=0.05)
            except TimeoutError:
                pass
            wake.clear()

            now = time.monotonic()
            now_ms = int(time.time() * 1000)
            book = mexc.books.get(SYMBOL)
            snap = model.snapshot(now_ms=now_ms, threshold_bps=0.0)

            if book is None:
                latest_decision = None
                latest_status = "no_mexc_book"
            else:
                latest_book_age = now_ms - book.recv_ms
                latest_spread = book.spread_bps
                if latest_book_age > frozen.max_book_age_ms:
                    latest_decision = None
                    latest_status = f"stale_mexc_book>{frozen.max_book_age_ms:.0f}ms"
                elif not fees.fresh_zero(SYMBOL, now_ms):
                    latest_decision = None
                    latest_status = "fee_not_fresh_exact_zero"
                elif not v2._valid_snapshot(snap):
                    latest_decision = None
                    latest_status = "invalid_binance_mexc_snapshot"
                else:
                    d = gate.observe(SYMBOL, snap, book.spread_bps, now_ms, event_key=v2._event_key(model))
                    latest_decision = d
                    strength = abs(d.residual_bps) / max(d.threshold_bps, 1e-12)
                    if not d.ready:
                        latest_status = d.reason
                    elif strength < frozen.min_signal_strength_ratio:
                        latest_status = f"strength<{frozen.min_signal_strength_ratio:.1f}x"
                    elif abs(d.residual_bps) < frozen.min_absolute_residual_bps:
                        latest_status = f"residual<{frozen.min_absolute_residual_bps:.1f}bps"
                    else:
                        latest_status = "READY"

            if now >= next_print:
                next_print = now + max(0.1, args.interval)
                if latest_decision is None:
                    console.print(
                        f"BTW WATCH spread={latest_spread:.2f}bps book_age={latest_book_age}ms "
                        f"STATUS={latest_status}"
                    )
                else:
                    d = latest_decision
                    strength = abs(d.residual_bps) / max(d.threshold_bps, 1e-12)
                    console.print(
                        f"BTW WATCH residual={d.residual_bps:+.2f}bps threshold={d.threshold_bps:.2f}bps "
                        f"strength={strength:.2f}x spread={latest_spread:.2f}bps "
                        f"binance_move={d.binance_move_bps:+.2f}bps mexc_move={d.mexc_move_bps:+.2f}bps "
                        f"leader_adv={d.leader_advantage_bps:+.2f}bps gate_ready={d.ready} "
                        f"STATUS={latest_status}"
                    )
    finally:
        fee_stop.set()
        fee_task.cancel()
        try:
            await fee_task
        except asyncio.CancelledError:
            pass
        await binance.close()
        await mexc.close()


def main() -> None:
    args = build_parser().parse_args()
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
