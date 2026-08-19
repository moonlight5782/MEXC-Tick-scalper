from __future__ import annotations

import asyncio
from dataclasses import dataclass

from rich.console import Console

from . import persistent_end2end_shadow as runner
from .baseline_v1 import apply_baseline_v1
from .prelive_persistent_catchup_shadow import impulse_retention_fraction


console = Console()
START_BANK_USDT = 100.0
LEVERAGE = 200.0
EMERGENCY_ADVERSE_BPS = 0.01  # first meaningful adverse tick; exit still pays measured latency


@dataclass(slots=True)
class BankState:
    balance_usdt: float = START_BANK_USDT
    last_requested_notional_usdt: float = 0.0
    last_filled_notional_usdt: float = 0.0
    last_isolated_margin_usdt: float = 0.0

    @property
    def buying_power_usdt(self) -> float:
        return max(0.0, self.balance_usdt) * LEVERAGE


BANK = BankState()


def economic_arrival_entry_ok(
    *,
    signal,
    current_residual_bps: float,
    current_binance_price: float,
    current_spread_bps: float,
    min_residual_retention: float,
    min_impulse_retention: float,
    min_remaining_edge_bps: float,
    min_edge_after_spread_bps: float,
):
    """Arrival gate for the BTW real-data shadow.

    Retention ratios are diagnostics only. They must not reject a trade when a
    large absolute residual still survives the measured latency. Hard rejection
    here is limited to a residual direction reversal or too little absolute edge.
    Depth, IOC slippage and full round-trip executable cost are checked by the
    base runner immediately after this function returns True.
    """
    del min_residual_retention, min_impulse_retention

    if signal.direction * current_residual_bps <= 0:
        return False, "residual_reversed", 0.0, 0.0

    residual_retention = abs(current_residual_bps) / max(abs(signal.residual_bps), 1e-12)
    impulse_retention = impulse_retention_fraction(
        signal.direction,
        signal.binance_price,
        signal.binance_move_bps,
        current_binance_price,
    )

    required = max(
        float(min_remaining_edge_bps),
        max(0.0, float(current_spread_bps)) + max(0.0, float(min_edge_after_spread_bps)),
    )
    if abs(float(current_residual_bps)) < required:
        return False, "remaining_edge_too_small", residual_retention, impulse_retention

    return True, "absolute_edge_survived", residual_retention, impulse_retention


def _bank_sized_virtual_ioc_fill(
    book,
    *,
    direction: int,
    target_notional_usdt: float,
    contract_size: float,
    cross_bps: float,
):
    """Size each simulated IOC from current isolated buying power.

    The account starts with 100 USDT. At 200x the maximum requested notional is
    current equity * 200. The real LIVE MEXC depth walker may fill only a fraction
    of that amount, so isolated margin is based on actual filled notional.
    """
    del target_notional_usdt
    requested = BANK.buying_power_usdt
    BANK.last_requested_notional_usdt = requested
    fill = _ORIGINAL_VIRTUAL_IOC_FILL(
        book,
        direction=direction,
        target_notional_usdt=requested,
        contract_size=contract_size,
        cross_bps=cross_bps,
    )
    filled = max(0.0, float(fill.qty) * float(fill.avg_price))
    BANK.last_filled_notional_usdt = filled
    BANK.last_isolated_margin_usdt = filled / LEVERAGE if LEVERAGE > 0 else 0.0
    return fill


def _bank_record_close(stats, row) -> None:
    """Apply closed PnL to the simulated isolated account.

    An isolated position cannot consume more than its assigned margin. Because
    this read-only shadow cannot observe the exchange's exact internal liquidation
    engine without placing an order, losses are conservatively capped at the
    position's isolated margin for bankroll accounting. The emergency adverse
    exit normally fires much earlier; measured exit latency still applies.
    """
    margin = max(0.0, float(row.filled_notional_usdt) / LEVERAGE)
    raw_pnl = float(row.pnl_usdt)
    accounted_pnl = max(raw_pnl, -margin)
    liquidated = accounted_pnl > raw_pnl + 1e-12
    if liquidated:
        row.pnl_usdt = accounted_pnl
        row.pnl_bps = accounted_pnl / max(float(row.filled_notional_usdt), 1e-12) * 10_000.0
        row.exit_reason = f"isolated_liquidation_cap_after_{row.exit_reason}"

    balance_before = BANK.balance_usdt
    _ORIGINAL_RECORD_CLOSE(stats, row)
    BANK.balance_usdt = max(0.0, balance_before + float(row.pnl_usdt))
    roe = float(row.pnl_usdt) / max(margin, 1e-12) * 100.0 if margin > 0 else 0.0
    console.print(
        f"[bold]BANK[/bold] before=${balance_before:.2f} margin=${margin:.2f} leverage={LEVERAGE:.0f}x "
        f"notional=${row.filled_notional_usdt:.2f} pnl=${row.pnl_usdt:+.2f} ROE={roe:+.1f}% "
        f"after=${BANK.balance_usdt:.2f} next_buying_power=${BANK.buying_power_usdt:.2f}"
    )


def _bank_run_budget_open(now, deadline, stats, args, trades) -> bool:
    return BANK.balance_usdt > 0.0 and _ORIGINAL_RUN_BUDGET_OPEN(now, deadline, stats, args, trades)


_ORIGINAL_VIRTUAL_IOC_FILL = runner.virtual_ioc_fill
_ORIGINAL_RECORD_CLOSE = runner._record_close
_ORIGINAL_RUN_BUDGET_OPEN = runner._run_budget_open


async def run(args):
    BANK.balance_usdt = START_BANK_USDT
    BANK.last_requested_notional_usdt = 0.0
    BANK.last_filled_notional_usdt = 0.0
    BANK.last_isolated_margin_usdt = 0.0

    # Restore the previously agreed trading/risk behaviour.
    args.target_notional_usdt = START_BANK_USDT * LEVERAGE
    args.min_hold_ms = 0
    args.mid_adverse_cut_bps = EMERGENCY_ADVERSE_BPS
    # A zero configured floor means the existing PositiveTrailing uses the
    # current LIVE spread as its trailing distance: max(0, current_spread).
    args.trailing_distance_bps = 0.0

    original_gate = runner.delayed_catchup_entry_ok
    original_fill = runner.virtual_ioc_fill
    original_record = runner._record_close
    original_budget = runner._run_budget_open
    runner.delayed_catchup_entry_ok = economic_arrival_entry_ok
    runner.virtual_ioc_fill = _bank_sized_virtual_ioc_fill
    runner._record_close = _bank_record_close
    runner._run_budget_open = _bank_run_budget_open
    try:
        console.print("[bold cyan]BTW FINAL RISK MODEL[/bold cyan]")
        console.print(
            f"bank=${START_BANK_USDT:.2f} isolated margin leverage={LEVERAGE:.0f}x "
            f"initial buying power=${START_BANK_USDT * LEVERAGE:.0f}"
        )
        console.print(
            "sizing=current bank*200x, capped by executable LIVE MEXC depth; "
            "margin=actual filled notional/200"
        )
        console.print(
            "exit protection=first adverse move -> sticky emergency exit with CURRENT measured exit latency; "
            "positive trailing distance=LIVE spread"
        )
        rows = await runner.run(args)
        console.print(
            f"[bold cyan]FINAL BANK[/bold cyan] start=${START_BANK_USDT:.2f} "
            f"end=${BANK.balance_usdt:.2f} net=${BANK.balance_usdt-START_BANK_USDT:+.2f} "
            f"buying_power=${BANK.buying_power_usdt:.2f}"
        )
        return rows
    finally:
        runner.delayed_catchup_entry_ok = original_gate
        runner.virtual_ioc_fill = original_fill
        runner._record_close = original_record
        runner._run_budget_open = original_budget


def main() -> None:
    args = runner.build_parser().parse_args()
    apply_baseline_v1(args)
    if args.target_closed_trades <= 0:
        raise SystemExit("--target-closed-trades must be positive")
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
