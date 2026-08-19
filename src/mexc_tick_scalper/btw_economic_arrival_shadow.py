from __future__ import annotations

import asyncio
from dataclasses import dataclass

from rich.console import Console

from . import persistent_end2end_shadow as runner
from .baseline_v1 import apply_baseline_v1
from .live_zero_fee_universe import discover_live_zero_fee_crosslisted
from .prelive_persistent_catchup_shadow import impulse_retention_fraction


console = Console()
SYMBOL = "BTW_USDT"
START_BANK_USDT = 100.0
REQUESTED_LEVERAGE = 200.0
EFFECTIVE_LEVERAGE = REQUESTED_LEVERAGE
MAX_ISOLATED_MARGIN_FRACTION = 0.10
MAX_SESSION_DRAWDOWN_FRACTION = 0.20
EMERGENCY_ADVERSE_BPS = 0.01


@dataclass(slots=True)
class BankState:
    balance_usdt: float = START_BANK_USDT
    last_requested_notional_usdt: float = 0.0
    last_filled_notional_usdt: float = 0.0
    last_isolated_margin_usdt: float = 0.0

    @property
    def max_isolated_margin_usdt(self) -> float:
        return max(0.0, self.balance_usdt) * MAX_ISOLATED_MARGIN_FRACTION

    @property
    def buying_power_usdt(self) -> float:
        return self.max_isolated_margin_usdt * EFFECTIVE_LEVERAGE

    @property
    def drawdown_stop_balance(self) -> float:
        return START_BANK_USDT * (1.0 - MAX_SESSION_DRAWDOWN_FRACTION)


BANK = BankState()


class GuardedLatencyProvider(runner.LatencyProvider):
    def entry(self):
        if self.replay:
            return super().entry()
        assert self.probe is not None
        snap = self.probe.snapshot()
        if snap is None or snap.inflight_timed_out or snap.age_seconds() > self.max_age_seconds:
            return None
        return runner.EntryLatency(snap.value(self.profile), snap.age_seconds() * 1000.0, None)

    def exit(self, replay_exit_ms):
        if replay_exit_ms is not None:
            return runner.ExitLatency(replay_exit_ms, 0.0, False)
        assert self.probe is not None
        snap = self.probe.snapshot()
        if snap is None or snap.inflight_timed_out or snap.age_seconds() > self.max_age_seconds:
            return None
        age_ms = snap.age_seconds() * 1000.0
        return runner.ExitLatency(snap.value(self.profile), age_ms, False)

    def status(self) -> str:
        if self.replay:
            return self.mode
        assert self.probe is not None
        snap = self.probe.snapshot()
        if snap is None:
            return f"REALTIME warming error={self.probe.last_error or '-'}"
        age_ms = snap.age_seconds() * 1000.0
        if snap.inflight_timed_out or age_ms > self.max_age_seconds * 1000.0:
            return (
                f"REALTIME STALE/BLOCKED latest={snap.latest_ms:.1f}ms age={age_ms:.0f}ms "
                f"error={self.probe.last_error or 'awaiting fresh sample'}"
            )
        effective = snap.value(self.profile)
        return (
            f"REALTIME n={snap.samples} latest={snap.latest_ms:.1f}ms median={snap.median_ms:.1f}ms "
            f"p75={snap.p75_ms:.1f}ms p95={snap.p95_ms:.1f}ms inflight={snap.inflight_ms:.1f}ms "
            f"effective={effective:.1f}ms age={age_ms:.0f}ms"
        )


def economic_arrival_entry_ok(
    *, signal, current_residual_bps: float, current_binance_price: float,
    current_spread_bps: float, min_residual_retention: float,
    min_impulse_retention: float, min_remaining_edge_bps: float,
    min_edge_after_spread_bps: float,
):
    del min_residual_retention, min_impulse_retention
    if signal.direction * current_residual_bps <= 0:
        return False, "residual_reversed", 0.0, 0.0
    residual_retention = abs(current_residual_bps) / max(abs(signal.residual_bps), 1e-12)
    impulse_retention = impulse_retention_fraction(
        signal.direction, signal.binance_price, signal.binance_move_bps, current_binance_price
    )
    required = max(
        float(min_remaining_edge_bps),
        max(0.0, float(current_spread_bps)) + max(0.0, float(min_edge_after_spread_bps)),
    )
    if abs(float(current_residual_bps)) < required:
        return False, (
            "remaining_edge_too_small"
            f"[arrival_residual={abs(float(current_residual_bps)):.2f}bps"
            f",arrival_spread={float(current_spread_bps):.2f}bps"
            f",required={required:.2f}bps]"
        ), residual_retention, impulse_retention
    return True, "absolute_edge_survived", residual_retention, impulse_retention


def _bank_sized_virtual_ioc_fill(
    book, *, direction: int, target_notional_usdt: float,
    contract_size: float, cross_bps: float,
):
    del target_notional_usdt
    requested = BANK.buying_power_usdt
    BANK.last_requested_notional_usdt = requested
    console.print(
        f"[cyan]RISK SIZE[/cyan] {SYMBOL} bank=${BANK.balance_usdt:.2f} "
        f"margin_cap={MAX_ISOLATED_MARGIN_FRACTION:.0%}=${BANK.max_isolated_margin_usdt:.2f} "
        f"leverage={EFFECTIVE_LEVERAGE:.0f}x max_notional=${requested:.2f}"
    )
    fill = _ORIGINAL_VIRTUAL_IOC_FILL(
        book, direction=direction, target_notional_usdt=requested,
        contract_size=contract_size, cross_bps=cross_bps,
    )
    filled = max(0.0, float(fill.qty) * float(fill.avg_price))
    BANK.last_filled_notional_usdt = filled
    BANK.last_isolated_margin_usdt = filled / EFFECTIVE_LEVERAGE if EFFECTIVE_LEVERAGE > 0 else 0.0
    return fill


def _bank_record_close(stats, row) -> None:
    margin = max(0.0, float(row.filled_notional_usdt) / EFFECTIVE_LEVERAGE)
    raw_pnl = float(row.pnl_usdt)
    accounted_pnl = max(raw_pnl, -margin)
    if accounted_pnl > raw_pnl + 1e-12:
        row.pnl_usdt = accounted_pnl
        row.pnl_bps = accounted_pnl / max(float(row.filled_notional_usdt), 1e-12) * 10_000.0
        row.exit_reason = f"isolated_liquidation_cap_after_{row.exit_reason}"
    balance_before = BANK.balance_usdt
    _ORIGINAL_RECORD_CLOSE(stats, row)
    BANK.balance_usdt = max(0.0, balance_before + float(row.pnl_usdt))
    roe = float(row.pnl_usdt) / max(margin, 1e-12) * 100.0 if margin > 0 else 0.0
    console.print(
        f"[bold]BANK[/bold] before=${balance_before:.2f} margin=${margin:.2f} "
        f"leverage={EFFECTIVE_LEVERAGE:.0f}x notional=${row.filled_notional_usdt:.2f} "
        f"pnl=${row.pnl_usdt:+.2f} ROE={roe:+.1f}% after=${BANK.balance_usdt:.2f} "
        f"next_position_margin_cap=${BANK.max_isolated_margin_usdt:.2f}"
    )
    if BANK.balance_usdt <= BANK.drawdown_stop_balance:
        console.print(
            f"[bold red]SESSION KILL SWITCH[/bold red] balance=${BANK.balance_usdt:.2f} "
            f"<= ${BANK.drawdown_stop_balance:.2f}; no new entries"
        )


def _bank_run_budget_open(now, deadline, stats, args, trades) -> bool:
    return (
        BANK.balance_usdt > BANK.drawdown_stop_balance
        and _ORIGINAL_RUN_BUDGET_OPEN(now, deadline, stats, args, trades)
    )


_ORIGINAL_VIRTUAL_IOC_FILL = runner.virtual_ioc_fill
_ORIGINAL_RECORD_CLOSE = runner._record_close
_ORIGINAL_RUN_BUDGET_OPEN = runner._run_budget_open
_ORIGINAL_LATENCY_PROVIDER = runner.LatencyProvider


async def _resolve_effective_leverage() -> float:
    contracts = await discover_live_zero_fee_crosslisted()
    contract = next((row for row in contracts if row.mexc_symbol == SYMBOL), None)
    if contract is None:
        raise RuntimeError(f"{SYMBOL} is not currently in the LIVE exact-0/0 cross-listed universe")
    return max(1.0, min(REQUESTED_LEVERAGE, float(contract.max_leverage)))


async def run(args):
    global EFFECTIVE_LEVERAGE
    EFFECTIVE_LEVERAGE = await _resolve_effective_leverage()
    BANK.balance_usdt = START_BANK_USDT
    BANK.last_requested_notional_usdt = 0.0
    BANK.last_filled_notional_usdt = 0.0
    BANK.last_isolated_margin_usdt = 0.0

    args.target_notional_usdt = START_BANK_USDT * MAX_ISOLATED_MARGIN_FRACTION * EFFECTIVE_LEVERAGE
    args.min_hold_ms = 0
    args.mid_adverse_cut_bps = EMERGENCY_ADVERSE_BPS
    args.trailing_distance_bps = 0.0

    original_gate = runner.delayed_catchup_entry_ok
    original_fill = runner.virtual_ioc_fill
    original_record = runner._record_close
    original_budget = runner._run_budget_open
    original_latency_provider = runner.LatencyProvider
    runner.delayed_catchup_entry_ok = economic_arrival_entry_ok
    runner.virtual_ioc_fill = _bank_sized_virtual_ioc_fill
    runner._record_close = _bank_record_close
    runner._run_budget_open = _bank_run_budget_open
    runner.LatencyProvider = GuardedLatencyProvider
    try:
        console.print("[bold cyan]BTW FINAL RISK MODEL[/bold cyan]")
        console.print(
            f"bank=${START_BANK_USDT:.2f} isolated requested_leverage={REQUESTED_LEVERAGE:.0f}x "
            f"effective={EFFECTIVE_LEVERAGE:.0f}x max_margin_per_trade={MAX_ISOLATED_MARGIN_FRACTION:.0%} "
            f"session_kill_drawdown={MAX_SESSION_DRAWDOWN_FRACTION:.0%}"
        )
        rows = await runner.run(args)
        console.print(
            f"[bold cyan]FINAL BANK[/bold cyan] start=${START_BANK_USDT:.2f} "
            f"end=${BANK.balance_usdt:.2f} net=${BANK.balance_usdt-START_BANK_USDT:+.2f}"
        )
        return rows
    finally:
        runner.delayed_catchup_entry_ok = original_gate
        runner.virtual_ioc_fill = original_fill
        runner._record_close = original_record
        runner._run_budget_open = original_budget
        runner.LatencyProvider = original_latency_provider


def main() -> None:
    args = runner.build_parser().parse_args()
    apply_baseline_v1(args)
    if args.target_closed_trades <= 0:
        raise SystemExit("--target-closed-trades must be positive")
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
