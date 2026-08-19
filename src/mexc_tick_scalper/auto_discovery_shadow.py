from __future__ import annotations

import asyncio
import csv
import math
import time
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from rich.console import Console
from rich.table import Table

from . import persistent_end2end_shadow as runner
from .baseline_v1 import apply_baseline_v1
from .live_zero_fee_universe import LiveZeroFeeContract, discover_live_zero_fee_crosslisted
from .persistent_lag_profile import PairLagProfile, build_profiles
from .prelive_persistent_catchup_shadow import impulse_retention_fraction
from .realtime_latency import RealtimeLatencyProbe

console = Console()
START_BANK_USDT = 100.0
REQUESTED_LEVERAGE = 200.0
EMERGENCY_ADVERSE_BPS = 0.01
DISCOVERY_LATENCY_TIMEOUT_S = 12.0


@dataclass(frozen=True, slots=True)
class Candidate:
    profile: PairLagProfile
    contract: LiveZeroFeeContract
    current_survival: float
    score: float


@dataclass(slots=True)
class BankState:
    balance_usdt: float = START_BANK_USDT


BANK = BankState()
CURRENT_SYMBOL = ""
CONTRACTS: dict[str, LiveZeroFeeContract] = {}
SELECTED_PROFILES: list[PairLagProfile] = []


def _load_lifetimes(path: Path) -> dict[str, list[float]]:
    signals: dict[str, str] = {}
    lifetimes: dict[str, list[float]] = defaultdict(list)
    with path.open("r", newline="", encoding="utf-8-sig") as handle:
        for row in csv.DictReader(handle):
            signal_id = str(row.get("signal_id") or "").strip()
            event = str(row.get("event") or "").strip()
            if not signal_id:
                continue
            if event == "signal":
                symbol = str(row.get("symbol") or "").strip().upper()
                if symbol:
                    signals[signal_id] = symbol
            elif event == "terminal":
                symbol = str(row.get("symbol") or signals.get(signal_id) or "").strip().upper()
                try:
                    lifetime = float(row.get("lifetime_ms") or "nan")
                except (TypeError, ValueError):
                    continue
                if symbol and math.isfinite(lifetime) and lifetime >= 0:
                    lifetimes[symbol].append(lifetime)
    return dict(lifetimes)


def _survival_at(lifetimes: list[float], latency_ms: float) -> float:
    if not lifetimes:
        return 0.0
    return sum(1 for value in lifetimes if value >= latency_ms) / len(lifetimes)


def _score(profile: PairLagProfile, current_survival: float) -> float:
    # Rank executable persistence first, then reward strong, repeated, wide residuals.
    persistence = max(0.0, current_survival)
    residual = max(0.0, profile.median_signal_residual_bps)
    strength = max(0.0, profile.median_signal_strength_ratio)
    evidence = math.log1p(max(0, profile.signals))
    convergence_quality = max(0.10, profile.convergence_rate + 0.25 * (1.0 - profile.reversal_rate))
    return persistence * residual * strength * evidence * convergence_quality


async def _measure_discovery_latency(args) -> tuple[float, str]:
    probe = RealtimeLatencyProbe(
        interval_ms=args.latency_probe_interval_ms,
        window=args.latency_window,
        minimum_samples=args.latency_min_samples,
    )
    await probe.start()
    started = time.monotonic()
    try:
        while time.monotonic() - started < DISCOVERY_LATENCY_TIMEOUT_S:
            snap = probe.snapshot()
            if snap is not None and snap.age_seconds() <= args.latency_max_age_seconds:
                return snap.value(args.latency_profile), (
                    f"latest={snap.latest_ms:.1f} median={snap.median_ms:.1f} "
                    f"p75={snap.p75_ms:.1f} p95={snap.p95_ms:.1f}"
                )
            await asyncio.sleep(0.05)
    finally:
        await probe.close()
    raise RuntimeError("Could not obtain a fresh LIVE MEXC latency sample for discovery")


def _economic_arrival_entry_ok(
    *, signal, current_residual_bps: float, current_binance_price: float,
    current_spread_bps: float, min_residual_retention: float,
    min_impulse_retention: float, min_remaining_edge_bps: float,
    min_edge_after_spread_bps: float,
):
    global CURRENT_SYMBOL
    del min_residual_retention, min_impulse_retention
    CURRENT_SYMBOL = signal.symbol
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
        console.print(
            f"[yellow]ARRIVAL ECONOMICS[/yellow] {signal.symbol} residual={abs(current_residual_bps):.2f}bps "
            f"spread={current_spread_bps:.2f}bps required={required:.2f}bps "
            f"retention={residual_retention:.1%} impulse_retention={impulse_retention:.1%}"
        )
        return False, "remaining_edge_too_small", residual_retention, impulse_retention
    return True, "absolute_edge_survived", residual_retention, impulse_retention


def _effective_leverage(symbol: str) -> float:
    contract = CONTRACTS.get(symbol)
    if contract is None:
        return 1.0
    return max(1.0, min(REQUESTED_LEVERAGE, float(contract.max_leverage)))


def _auto_sized_virtual_ioc_fill(
    book, *, direction: int, target_notional_usdt: float,
    contract_size: float, cross_bps: float,
):
    del target_notional_usdt
    leverage = _effective_leverage(CURRENT_SYMBOL)
    requested = max(0.0, BANK.balance_usdt) * leverage
    return _ORIGINAL_VIRTUAL_IOC_FILL(
        book,
        direction=direction,
        target_notional_usdt=requested,
        contract_size=contract_size,
        cross_bps=cross_bps,
    )


def _auto_record_close(stats, row) -> None:
    leverage = _effective_leverage(row.symbol)
    margin = max(0.0, float(row.filled_notional_usdt) / leverage)
    raw_pnl = float(row.pnl_usdt)
    accounted_pnl = max(raw_pnl, -margin)
    if accounted_pnl > raw_pnl + 1e-12:
        row.pnl_usdt = accounted_pnl
        row.pnl_bps = accounted_pnl / max(float(row.filled_notional_usdt), 1e-12) * 10_000.0
        row.exit_reason = f"isolated_liquidation_cap_after_{row.exit_reason}"
    before = BANK.balance_usdt
    _ORIGINAL_RECORD_CLOSE(stats, row)
    BANK.balance_usdt = max(0.0, before + float(row.pnl_usdt))
    roe = float(row.pnl_usdt) / max(margin, 1e-12) * 100.0 if margin > 0 else 0.0
    console.print(
        f"[bold]BANK[/bold] {row.symbol} before=${before:.2f} leverage={leverage:.0f}x "
        f"margin=${margin:.2f} notional=${row.filled_notional_usdt:.2f} "
        f"pnl=${row.pnl_usdt:+.2f} ROE={roe:+.1f}% after=${BANK.balance_usdt:.2f}"
    )


def _auto_budget(now, deadline, stats, args, trades) -> bool:
    return BANK.balance_usdt > 0.0 and _ORIGINAL_BUDGET(now, deadline, stats, args, trades)


def _selected_profiles_override(profiles, **kwargs):
    del profiles, kwargs
    return list(SELECTED_PROFILES)


_ORIGINAL_VIRTUAL_IOC_FILL = runner.virtual_ioc_fill
_ORIGINAL_RECORD_CLOSE = runner._record_close
_ORIGINAL_BUDGET = runner._run_budget_open
_ORIGINAL_SELECT_PROFILES = runner.select_profiles


async def discover(args) -> list[Candidate]:
    source = Path(args.lifetime_csv) if args.lifetime_csv else runner.latest_lifetime_csv(Path.cwd())
    profiles = build_profiles(source)
    lifetimes = _load_lifetimes(source)
    contracts = await discover_live_zero_fee_crosslisted()
    contract_by_symbol = {row.mexc_symbol: row for row in contracts}
    latency_ms, latency_desc = await _measure_discovery_latency(args)

    console.print("[bold cyan]AUTO DISCOVERY[/bold cyan]")
    console.print(f"LIVE measured latency for ranking: {latency_ms:.1f}ms ({latency_desc})")
    candidates: list[Candidate] = []
    for profile in profiles:
        contract = contract_by_symbol.get(profile.symbol)
        if contract is None:
            continue
        current_survival = _survival_at(lifetimes.get(profile.symbol, []), latency_ms)
        if profile.signals < args.pair_min_signals:
            continue
        if profile.median_lifetime_ms < args.pair_min_median_lifetime_ms:
            continue
        if profile.median_signal_strength_ratio < args.pair_min_strength_ratio:
            continue
        if current_survival < args.pair_min_survival_rate:
            continue
        candidates.append(Candidate(profile, contract, current_survival, _score(profile, current_survival)))

    candidates.sort(key=lambda row: row.score, reverse=True)
    candidates = candidates[: max(1, int(args.discovery_top))]
    if not candidates:
        raise RuntimeError("AUTO DISCOVERY found no LIVE pair that passes current-latency persistence filters")

    table = Table(title="Current executable lag candidates")
    for col in ("#", "Symbol", "Signals", "Med lag", "Survive@RTT", "Residual", "Strength", "Max lev", "Score"):
        table.add_column(col)
    for idx, row in enumerate(candidates, 1):
        p = row.profile
        table.add_row(
            str(idx), p.symbol, str(p.signals), f"{p.median_lifetime_ms:.0f}ms",
            f"{row.current_survival:.0%}", f"{p.median_signal_residual_bps:.1f}bps",
            f"{p.median_signal_strength_ratio:.2f}x", f"{row.contract.max_leverage}x", f"{row.score:.1f}",
        )
    console.print(table)
    console.print("Selected: " + ", ".join(row.profile.symbol for row in candidates))
    return candidates


async def run(args):
    global CONTRACTS, SELECTED_PROFILES, CURRENT_SYMBOL
    candidates = await discover(args)
    CONTRACTS = {row.contract.mexc_symbol: row.contract for row in candidates}
    SELECTED_PROFILES = [row.profile for row in candidates]
    CURRENT_SYMBOL = ""
    BANK.balance_usdt = START_BANK_USDT

    args.min_hold_ms = 0
    args.mid_adverse_cut_bps = EMERGENCY_ADVERSE_BPS
    args.trailing_distance_bps = 0.0
    # Cosmetic fallback only; actual per-symbol request is bank * effective leverage.
    args.target_notional_usdt = START_BANK_USDT * REQUESTED_LEVERAGE

    original_gate = runner.delayed_catchup_entry_ok
    original_fill = runner.virtual_ioc_fill
    original_record = runner._record_close
    original_budget = runner._run_budget_open
    original_select = runner.select_profiles
    runner.delayed_catchup_entry_ok = _economic_arrival_entry_ok
    runner.virtual_ioc_fill = _auto_sized_virtual_ioc_fill
    runner._record_close = _auto_record_close
    runner._run_budget_open = _auto_budget
    runner.select_profiles = _selected_profiles_override
    try:
        console.print(
            f"[bold cyan]AUTO SHADOW RISK[/bold cyan] bank=${START_BANK_USDT:.2f} isolated "
            f"requested_leverage={REQUESTED_LEVERAGE:.0f}x; effective leverage=min(requested,LIVE max per symbol)"
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
        runner.select_profiles = original_select


def build_parser():
    p = runner.build_parser()
    p.description = "Automatic LIVE pair discovery + read-only E2E latency shadow"
    p.add_argument("--discovery-top", type=int, default=5)
    return p


def main() -> None:
    args = build_parser().parse_args()
    apply_baseline_v1(args)
    if args.discovery_top <= 0:
        raise SystemExit("--discovery-top must be positive")
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
