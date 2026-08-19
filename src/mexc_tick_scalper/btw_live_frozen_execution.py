from __future__ import annotations

import asyncio

from rich.console import Console

from . import live_production_runner_v2 as live_v2
from .execution import PositionSnapshot
from .lead_lag_strategy import LagDecision, LeadLagGate
from .web_execution import MexcWebError

console = Console()
SYMBOL = "BTW_USDT"
MIN_ABSOLUTE_RESIDUAL_BPS = 8.0
MIN_SIGNAL_STRENGTH_RATIO = 3.0


class FrozenBTWGate(LeadLagGate):
    """LeadLagGate with the validated frozen absolute residual/strength gates.

    Actual order latency is NOT simulated in LIVE mode: the IOC is submitted
    immediately and the real network/exchange path supplies execution latency.
    """

    def observe(self, symbol, snap, spread_bps, now_ms, *, event_key=None):  # type: ignore[override]
        d = super().observe(symbol, snap, spread_bps, now_ms, event_key=event_key)
        if not d.ready:
            return d

        strength = abs(d.residual_bps) / max(d.threshold_bps, 1e-12)
        if abs(d.residual_bps) < MIN_ABSOLUTE_RESIDUAL_BPS:
            return LagDecision(
                False,
                "frozen_absolute_residual",
                d.direction,
                d.residual_bps,
                d.threshold_bps,
                d.noise_bps,
                d.binance_move_bps,
                d.mexc_move_bps,
                d.leader_advantage_bps,
            )
        if strength < MIN_SIGNAL_STRENGTH_RATIO:
            return LagDecision(
                False,
                "frozen_signal_strength",
                d.direction,
                d.residual_bps,
                d.threshold_bps,
                d.noise_bps,
                d.binance_move_bps,
                d.mexc_move_bps,
                d.leader_advantage_bps,
            )
        return d


async def _resolve_fill_without_position_poll(adapter, symbol, side, fill, leverage):
    """Use the confirmed IOC fill immediately instead of another private GET loop.

    If MEXC returned positionId with the fill, the exit path can submit the exact
    reduce-only close immediately from this cached snapshot. If positionId is
    absent, the existing close adapter still has its safe GET fallback.
    """
    if fill.filled_qty <= 0:
        raise MexcWebError("IOC returned no fill")
    return PositionSnapshot(
        symbol=symbol,
        side=side,
        qty=fill.filled_qty,
        entry_price=fill.avg_price,
        leverage=leverage,
        isolated=True,
        position_id=fill.position_id,
    )


def build_parser():
    p = live_v2.build_parser()
    p.description = "BTW_USDT-only real-money execution with frozen validated entry gates"
    return p


async def run(args) -> None:
    # Hard scope boundary: this runner can trade BTW_USDT only.
    args.include_symbols = SYMBOL
    args.exclude_symbols = ""

    # Frozen alpha parameters from baseline v1.
    args.micro_horizon_ms = 100
    args.baseline_seconds = 8.0
    args.baseline_exclusion_ms = 1000
    args.noise_window_ms = 8000
    args.residual_noise_multiplier = 3.0
    args.binance_noise_multiplier = 1.5
    args.min_edge_bps = 2.0
    args.min_net_edge_bps = 0.5
    args.edge_to_spread_ratio = 1.2
    args.min_binance_move_bps = 1.0
    args.min_leader_advantage_bps = 1.0
    args.min_lead_ratio = 1.35
    args.confirm_updates = 2
    args.confirm_ms = 15
    args.rearm_fraction = 0.35
    args.max_binance_age_ms = 300.0
    args.max_mexc_age_ms = 2000.0
    args.max_book_age_ms = 750.0
    args.ioc_cross_bps = 1.0
    args.entry_cooldown_ms = 0

    # Exit values aligned as closely as the existing LIVE execution engine permits.
    args.min_hold_seconds = 0.05
    args.max_hold_seconds = 15.0
    args.convergence_bps = 0.25
    args.convergence_fraction = 0.25
    args.reversal_edge_bps = 0.75
    args.adverse_cut_bps = 3.0
    args.adverse_spread_multiple = 1.0
    args.trailing_distance_bps = 1.5

    # First-live-run hard safety caps.
    if args.target_notional_usdt <= 0 or args.target_notional_usdt > 10.0:
        raise SystemExit("BTW LIVE safety cap: --target-notional-usdt must be >0 and <=10 USDT")
    if args.leverage != 1:
        raise SystemExit("BTW LIVE safety cap: first validation run is locked to --leverage 1")
    if args.max_cycles <= 0 or args.max_cycles > 10:
        raise SystemExit("BTW LIVE safety cap: --max-cycles must be between 1 and 10")
    if args.max_session_loss_usdt <= 0 or args.max_session_loss_usdt > 2.0:
        raise SystemExit("BTW LIVE safety cap: --max-session-loss-usdt must be >0 and <=2 USDT")

    # Inject only the frozen candidate gate and low-latency post-fill resolver.
    original_gate = live_v2.LeadLagGate
    original_resolver = live_v2._resolve_remote_position
    live_v2.LeadLagGate = FrozenBTWGate
    live_v2._resolve_remote_position = _resolve_fill_without_position_poll
    try:
        console.print("[bold red]BTW LIVE FROZEN EXECUTION[/bold red]")
        console.print("ONLY BTW_USDT; REAL MEXC orders; LIVE Binance + LIVE MEXC market data")
        console.print(
            f"Frozen entry: abs(residual)>={MIN_ABSOLUTE_RESIDUAL_BPS:.1f}bps, "
            f"strength>={MIN_SIGNAL_STRENGTH_RATIO:.1f}x, confirm=2 updates/15ms"
        )
        console.print(
            f"Safety: target=${args.target_notional_usdt:.2f}, leverage={args.leverage}x, "
            f"max_cycles={args.max_cycles}, max_session_loss=${args.max_session_loss_usdt:.2f}"
        )
        console.print("No artificial entry delay; no post-fill position polling before strategy monitoring.")
        await live_v2.run(args)
    finally:
        live_v2.LeadLagGate = original_gate
        live_v2._resolve_remote_position = original_resolver


def main() -> None:
    args = build_parser().parse_args()
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
