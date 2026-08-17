from __future__ import annotations

import argparse
import asyncio
import math
import time

from . import demo_lead_lag_test as leadlag
from . import demo_hybrid_test as hybrid
from .execution import OrderSide
from .web_execution import MexcWebError


class EmergencyExecutableExitPolicy(hybrid.AsymmetricExitPolicy):
    """Preserve the staged trailing policy and add an executable-price panic cut.

    The price passed by demo_lead_lag_test is already the executable close-side
    best price, not mid/last.  Therefore this guard catches book blow-outs that
    a mid-price adverse stop can miss.
    """

    emergency_executable_cut_bps: float = 3.0

    def on_tick(self, *, price, liquidation_price, signal, age_seconds, signal_fresh=True):
        if price > 0 and self.entry_price > 0:
            raw = (price - self.entry_price) / self.entry_price * 10_000.0
            signed = raw if self.side > 0 else -raw
            if signed <= -abs(float(self.emergency_executable_cut_bps)):
                return "emergency_executable_cut"
        return super().on_tick(
            price=price,
            liquidation_price=liquidation_price,
            signal=signal,
            age_seconds=age_seconds,
            signal_fresh=signal_fresh,
        )


class TimedDemoAdapter(leadlag._FastLeadLagDemoAdapter):
    async def open_ioc(self, *args, **kwargs):
        marks: dict[str, float] = {}
        kwargs["timing_marks"] = marks
        started = time.time_ns() / 1_000_000.0
        fill = await super().open_ioc(*args, **kwargs)
        done = time.time_ns() / 1_000_000.0
        post_ms = marks.get("ioc_post_response_ms", done) - marks.get("ioc_post_start_ms", started)
        confirm_ms = marks.get("ioc_confirmed_ms", done) - marks.get("ioc_post_start_ms", started)
        hybrid.console.print(
            f"DEMO IOC LATENCY post={post_ms:.1f}ms confirmed={confirm_ms:.1f}ms "
            f"requested_qty={fill.requested_qty:g} filled_qty={fill.filled_qty:g} fee={fill.fee_usdt:g}"
        )
        return fill


async def run(args: argparse.Namespace) -> None:
    # Execution validation intentionally does NOT require Demo/LIVE 0/0 eligibility.
    # Demo's useful liquid contracts can carry fees.  Real entry+exit fees remain
    # included in DEMO_REPORTED_PNL by the underlying runner.  ZERO_FEE_PNL is
    # retained only as a secondary counterfactual for the production 0/0 universe.
    original_fee_gate = leadlag._fee_cache_allows_entry
    original_adapter = leadlag._FastLeadLagDemoAdapter
    original_policy = hybrid.AsymmetricExitPolicy

    def execution_gate(*_a, **_kw) -> bool:
        return True

    cut_bps = float(args.emergency_executable_cut_bps)

    class ConfiguredEmergencyPolicy(EmergencyExecutableExitPolicy):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            self.emergency_executable_cut_bps = cut_bps

    leadlag._fee_cache_allows_entry = execution_gate
    leadlag._FastLeadLagDemoAdapter = TimedDemoAdapter
    hybrid.AsymmetricExitPolicy = ConfiguredEmergencyPolicy

    # 0 means current MEXC maximum in our product contract.  The legacy Demo
    # runner caps requested leverage by contract max, so a very high request
    # produces exactly the contract maximum.
    if int(args.leverage) <= 0:
        args.leverage = 1_000_000

    hybrid.console.print(
        "[bold cyan]DEMO EXECUTION VALIDATION V1[/bold cyan] "
        f"isolated_margin=${args.target_margin_usdt:.2f}/trade; leverage=MEXC_MAX; "
        f"emergency_executable_cut={cut_bps:.2f}bps"
    )
    hybrid.console.print(
        "TESTNET writes only. IOC partial fills are accepted, remainder cancels, no top-up. "
        "Primary execution PnL is DEMO_REPORTED_PNL after BOTH entry and exit fees."
    )
    hybrid.console.print(
        "ZERO_FEE_PNL is secondary only: it estimates the same fills with 0/0 fees for the LIVE exact-zero-fee universe."
    )

    try:
        await leadlag.run(args)
    finally:
        leadlag._fee_cache_allows_entry = original_fee_gate
        leadlag._FastLeadLagDemoAdapter = original_adapter
        hybrid.AsymmetricExitPolicy = original_policy


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Real MEXC TESTNET IOC/reduce-only execution validation with LIVE Binance->MEXC signals"
    )
    p.add_argument("--symbol", required=True)
    p.add_argument("--session-seconds", type=int, default=1800)
    p.add_argument("--max-cycles", type=int, default=20)
    p.add_argument("--leverage", type=int, default=0, help="0 = current MEXC max leverage")
    p.add_argument("--target-margin-usdt", type=float, default=60.0)
    p.add_argument("--lead-horizon-ms", type=int, default=100)
    p.add_argument("--baseline-seconds", type=float, default=8.0)
    p.add_argument("--warmup-seconds", type=float, default=10.0)
    p.add_argument("--min-edge-bps", type=float, default=8.0)
    p.add_argument("--min-net-edge-bps", type=float, default=2.0)
    p.add_argument("--min-binance-move-bps", type=float, default=1.0)
    p.add_argument("--max-quote-age-ms", type=float, default=300.0)
    p.add_argument("--max-live-book-age-ms", type=float, default=750.0)
    p.add_argument("--edge-to-spread-ratio", type=float, default=1.5)
    p.add_argument("--max-demo-live-divergence-bps", type=float, default=100000.0)
    p.add_argument("--convergence-bps", type=float, default=0.25)
    p.add_argument("--convergence-fraction", type=float, default=0.25)
    p.add_argument("--reversal-edge-bps", type=float, default=0.75)
    p.add_argument("--max-hold-seconds", type=float, default=15.0)
    p.add_argument("--fee-check-seconds", type=float, default=5.0)
    p.add_argument("--max-fee-age-seconds", type=float, default=8.0)
    p.add_argument("--pending-reconcile-seconds", type=float, default=5.0)
    p.add_argument("--pending-poll-seconds", type=float, default=0.15)
    p.add_argument("--heartbeat-seconds", type=float, default=2.0)
    p.add_argument("--early-adverse-changes", type=int, default=2)
    p.add_argument("--winner-arm-bps", type=float, default=0.5)
    p.add_argument("--winner-pullback-bps", type=float, default=1.5)
    p.add_argument("--exit-flip-confidence", type=float, default=0.30)
    p.add_argument("--exit-fade-confidence", type=float, default=0.12)
    p.add_argument("--min-hold-seconds", type=float, default=0.05)
    p.add_argument("--liq-buffer-fraction", type=float, default=0.25)
    p.add_argument("--emergency-executable-cut-bps", type=float, default=3.0)
    return p


def main() -> None:
    args = build_parser().parse_args()
    if args.target_margin_usdt <= 0:
        raise SystemExit("--target-margin-usdt must be > 0")
    if args.emergency_executable_cut_bps <= 0:
        raise SystemExit("--emergency-executable-cut-bps must be > 0")
    try:
        asyncio.run(run(args))
    except MexcWebError as exc:
        hybrid.console.print(f"[red]DEMO EXECUTION VALIDATION FAILED:[/red] {exc}")
        raise SystemExit(2) from exc


if __name__ == "__main__":
    main()
