from __future__ import annotations

import argparse
import asyncio
import csv
import math
import statistics
import time
from dataclasses import dataclass, field
from pathlib import Path

from rich.console import Console

from . import prelive_persistent_ioc_shadow_v2 as v2
from .baseline_v1 import apply_baseline_v1
from .lead_lag_strategy import LeadLagGate
from .live_lead_lag_shadow import PositiveTrailing
from .live_production_runner import FeeCache, _fee_loop, _signed_move_bps
from .live_zero_fee_universe import LiveZeroFeeContract, discover_live_zero_fee_crosslisted
from .microspread import MicroSpreadModel
from .microspread_feed import EventBinanceBookTickerFeed, EventMexcDepthFeed
from .persistent_lag_profile import build_profiles, latest_lifetime_csv, select_profiles
from .prelive_latency_diagnostic import _exit_depth_for_qty
from .prelive_persistent_catchup_shadow import Signal, delayed_catchup_entry_ok, directional_move_bps
from .prelive_persistent_ioc_shadow import immediate_roundtrip_cost_bps, virtual_ioc_fill
from .realtime_latency import RealtimeLatencyProbe

console = Console()
REFERENCE_COMMIT = "8a0bc6043385dbaf95ec8e77b93d91fd00a7f9e5"


@dataclass(frozen=True, slots=True)
class LatencySample:
    entry_ms: float
    exit_ms: float


@dataclass(frozen=True, slots=True)
class EntryLatency:
    value_ms: float
    age_ms: float
    replay_exit_ms: float | None = None


@dataclass(frozen=True, slots=True)
class ExitLatency:
    value_ms: float
    age_ms: float
    stale_fallback: bool = False


@dataclass(slots=True)
class PendingEntry:
    signal: Signal
    execute_at: float
    scheduled_arrival_ms: int
    latency_ms: float
    latency_age_ms: float
    replay_exit_ms: float | None


@dataclass(slots=True)
class PendingExit:
    reason: str
    decision_ms: int
    execute_at: float
    scheduled_arrival_ms: int
    latency_ms: float
    latency_age_ms: float
    stale_fallback: bool
    arrived: bool = False
    actual_arrival_ms: int | None = None


@dataclass(slots=True)
class Position:
    signal: Signal
    entry_ts_ms: int
    entry_scheduled_arrival_ms: int
    original_qty: float
    remaining_qty: float
    entry_price: float
    entry_mid: float
    entry_binance_price: float
    entry_residual_bps: float
    entry_notional: float
    entry_fill_ratio: float
    entry_latency_ms: float
    entry_latency_age_ms: float
    replay_exit_ms: float | None
    trailing: PositiveTrailing
    exit_pending: PendingExit | None = None
    realized_pnl_usdt: float = 0.0
    last_exit_book_recv_ms: int = 0


@dataclass(slots=True)
class TradeRow:
    symbol: str
    direction: int
    signal_ms: int
    entry_scheduled_arrival_ms: int
    entry_arrival_ms: int
    entry_schedule_overrun_ms: int
    exit_decision_ms: int
    exit_scheduled_arrival_ms: int
    exit_arrival_ms: int
    exit_schedule_overrun_ms: int
    close_ms: int
    exit_book_wait_ms: int
    measured_entry_latency_ms: float
    entry_latency_age_ms: float
    measured_exit_latency_ms: float
    exit_latency_age_ms: float
    exit_latency_stale_fallback: bool
    entry_price: float
    exit_vwap: float
    requested_notional_usdt: float
    filled_notional_usdt: float
    fill_ratio: float
    pnl_usdt: float
    pnl_bps: float
    hold_from_entry_ms: int
    signal_to_close_ms: int
    exit_reason: str


@dataclass(slots=True)
class Stats:
    signals: int = 0
    entries: int = 0
    expired: int = 0
    no_fill: int = 0
    wins: int = 0
    losses: int = 0
    flats: int = 0
    pnl_usdt: float = 0.0
    gross_win_usdt: float = 0.0
    gross_loss_usdt: float = 0.0
    fills: list[float] = field(default_factory=list)
    notionals: list[float] = field(default_factory=list)
    holds: list[float] = field(default_factory=list)
    signal_to_close: list[float] = field(default_factory=list)
    entry_overruns: list[float] = field(default_factory=list)
    exit_overruns: list[float] = field(default_factory=list)
    reasons: dict[str, int] = field(default_factory=dict)
    latency_rejects: int = 0

    @property
    def pf(self) -> float:
        if self.gross_loss_usdt <= 0:
            return math.inf if self.gross_win_usdt > 0 else 0.0
        return self.gross_win_usdt / self.gross_loss_usdt


def _summary(s: Stats) -> str:
    closed = s.wins + s.losses + s.flats
    wr = s.wins / closed * 100 if closed else 0.0
    pf = "inf" if math.isinf(s.pf) else f"{s.pf:.3f}"
    fill = statistics.median(s.fills) * 100 if s.fills else 0.0
    notion = statistics.median(s.notionals) if s.notionals else 0.0
    hold = statistics.median(s.holds) if s.holds else 0.0
    total = statistics.median(s.signal_to_close) if s.signal_to_close else 0.0
    entry_overrun = statistics.median(s.entry_overruns) if s.entry_overruns else 0.0
    exit_overrun = statistics.median(s.exit_overruns) if s.exit_overruns else 0.0
    reasons = ",".join(f"{k}:{v}" for k, v in sorted(s.reasons.items())) or "-"
    return (
        f"signals={s.signals} entries={s.entries} expired={s.expired} nofill={s.no_fill} "
        f"latency_rejects={s.latency_rejects} W/L/F={s.wins}/{s.losses}/{s.flats} "
        f"WR={wr:.1f}% PF_USDT={pf} pnl={s.pnl_usdt:+.4f}USDT fill_med={fill:.1f}% "
        f"notional_med=${notion:.0f} hold_med={hold:.0f}ms signal_to_close_med={total:.0f}ms "
        f"entry_overrun_med={entry_overrun:.0f}ms exit_overrun_med={exit_overrun:.0f}ms exits={reasons}"
    )


def _load_latency_samples(path: Path) -> list[LatencySample]:
    """Load historical paired entry/exit samples for explicit replay only."""
    out: list[LatencySample] = []
    with path.open("r", newline="", encoding="utf-8-sig") as handle:
        for row in csv.DictReader(handle):
            try:
                if row.get("signal_to_fill_ms") and row.get("exit_decision_to_fill_ms"):
                    entry = float(row["signal_to_fill_ms"])
                    exit_ = float(row["exit_decision_to_fill_ms"])
                elif row.get("signal_to_provisional_ms") and row.get("ioc_post_roundtrip_ms"):
                    entry = float(row["signal_to_provisional_ms"])
                    exit_ = float(row["ioc_post_roundtrip_ms"])
                elif row.get("signal_to_ioc_post_ms") and row.get("ioc_confirmation_ms") and row.get("ioc_post_roundtrip_ms"):
                    entry = float(row["signal_to_ioc_post_ms"]) + float(row["ioc_confirmation_ms"])
                    exit_ = float(row["ioc_post_roundtrip_ms"])
                else:
                    continue
            except (TypeError, ValueError):
                continue
            if entry > 0 and exit_ > 0 and math.isfinite(entry) and math.isfinite(exit_):
                out.append(LatencySample(entry, exit_))
    if not out:
        raise ValueError(f"No coherent entry/exit latency samples in {path}")
    return out


class LatencyProvider:
    """Realtime latency by default; historical CSV only when explicitly requested."""

    def __init__(self, args: argparse.Namespace) -> None:
        self.profile = args.latency_profile
        self.max_age_seconds = args.latency_max_age_seconds
        self.replay = _load_latency_samples(Path(args.latency_csv)) if args.latency_csv else []
        self.replay_index = 0
        self.probe = None if self.replay else RealtimeLatencyProbe(
            interval_ms=args.latency_probe_interval_ms,
            window=args.latency_window,
            minimum_samples=args.latency_min_samples,
        )

    @property
    def mode(self) -> str:
        return f"REPLAY:{len(self.replay)}" if self.replay else f"REALTIME:{self.profile}"

    async def start(self) -> None:
        if self.probe is not None:
            await self.probe.start()

    async def close(self) -> None:
        if self.probe is not None:
            await self.probe.close()

    def entry(self) -> EntryLatency | None:
        if self.replay:
            row = self.replay[self.replay_index % len(self.replay)]
            self.replay_index += 1
            return EntryLatency(row.entry_ms, 0.0, row.exit_ms)
        assert self.probe is not None
        snap = self.probe.snapshot()
        if snap is None or snap.age_seconds() > self.max_age_seconds:
            return None
        return EntryLatency(snap.value(self.profile), snap.age_seconds() * 1000.0, None)

    def exit(self, replay_exit_ms: float | None) -> ExitLatency | None:
        if replay_exit_ms is not None:
            return ExitLatency(replay_exit_ms, 0.0, False)
        assert self.probe is not None
        snap = self.probe.snapshot()
        if snap is None:
            return None
        age_ms = snap.age_seconds() * 1000.0
        return ExitLatency(
            snap.value(self.profile),
            age_ms,
            age_ms > self.max_age_seconds * 1000.0,
        )

    def status(self) -> str:
        if self.replay:
            return self.mode
        assert self.probe is not None
        snap = self.probe.snapshot()
        if snap is None:
            return f"REALTIME warming error={self.probe.last_error or '-'}"
        effective = snap.value(self.profile)
        return (
            f"REALTIME n={snap.samples} latest={snap.latest_ms:.1f}ms median={snap.median_ms:.1f}ms "
            f"p75={snap.p75_ms:.1f}ms p95={snap.p95_ms:.1f}ms inflight={snap.inflight_ms:.1f}ms "
            f"effective={effective:.1f}ms age={snap.age_seconds()*1000:.0f}ms"
        )


def _write_rows(path: Path, rows: list[TradeRow]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        fields = list(TradeRow.__dataclass_fields__)
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            data = {name: getattr(row, name) for name in fields}
            data["direction"] = "LONG" if row.direction > 0 else "SHORT"
            writer.writerow(data)


def _record_close(stats: Stats, row: TradeRow) -> None:
    pnl = row.pnl_usdt
    stats.pnl_usdt += pnl
    stats.holds.append(float(row.hold_from_entry_ms))
    stats.signal_to_close.append(float(row.signal_to_close_ms))
    stats.entry_overruns.append(float(row.entry_schedule_overrun_ms))
    stats.exit_overruns.append(float(row.exit_schedule_overrun_ms))
    stats.reasons[row.exit_reason] = stats.reasons.get(row.exit_reason, 0) + 1
    if pnl > 1e-9:
        stats.wins += 1
        stats.gross_win_usdt += pnl
    elif pnl < -1e-9:
        stats.losses += 1
        stats.gross_loss_usdt += abs(pnl)
    else:
        stats.flats += 1


def _run_budget_open(now: float, deadline: float, stats: Stats, args: argparse.Namespace, trades: list[TradeRow]) -> bool:
    return now < deadline and stats.signals < args.max_signals and len(trades) < args.target_closed_trades


def _must_drain(pending: PendingEntry | None, pos: Position | None) -> bool:
    return pending is not None or pos is not None


async def run(args: argparse.Namespace) -> list[TradeRow]:
    source = Path(args.lifetime_csv) if args.lifetime_csv else latest_lifetime_csv(Path.cwd())
    profiles = select_profiles(
        build_profiles(source),
        min_signals=args.pair_min_signals,
        min_median_lifetime_ms=args.pair_min_median_lifetime_ms,
        min_survival_rate=args.pair_min_survival_rate,
        min_signal_strength_ratio=args.pair_min_strength_ratio,
    )
    profile_by_symbol = {p.symbol: p for p in profiles}
    keep = set(profile_by_symbol)
    contracts = [x for x in await discover_live_zero_fee_crosslisted() if x.mexc_symbol in keep]
    if not contracts:
        raise RuntimeError("No frozen persistent pair is currently exact-0/0 and Binance-crosslisted")

    latency = LatencyProvider(args)
    await latency.start()
    console.print("[bold cyan]PERSISTENT END-TO-END LATENCY SHADOW[/bold cyan] - NO REAL ORDERS")
    console.print(f"Frozen reference: {REFERENCE_COMMIT}; lifetime source: {source.resolve()}")
    console.print(f"Latency source: {latency.mode}; no fixed latency constant is used in realtime mode.")
    console.print(
        "ENTRY and EXIT both execute on LIVE MEXC depth at modeled arrival. "
        "After EXIT DECISION, closing is sticky and never waits for Binance/alpha validity or a new MEXC depth update."
    )

    symbols = [x.mexc_symbol for x in contracts]
    by_symbol: dict[str, LiveZeroFeeContract] = {x.mexc_symbol: x for x in contracts}
    models = {
        x.mexc_symbol: MicroSpreadModel(
            horizon_ms=args.micro_horizon_ms,
            baseline_seconds=args.baseline_seconds,
            baseline_exclusion_ms=args.baseline_exclusion_ms,
            min_edge_bps=0.0,
            min_binance_move_bps=0.0,
            max_binance_age_ms=args.max_binance_age_ms,
            max_mexc_age_ms=args.max_mexc_age_ms,
        ) for x in contracts
    }
    gate = LeadLagGate(
        noise_window_ms=args.noise_window_ms,
        residual_noise_multiplier=args.residual_noise_multiplier,
        binance_noise_multiplier=args.binance_noise_multiplier,
        min_edge_bps=args.min_edge_bps,
        min_net_edge_bps=args.min_net_edge_bps,
        spread_ratio=args.edge_to_spread_ratio,
        min_binance_move_bps=args.min_binance_move_bps,
        min_leader_advantage_bps=args.min_leader_advantage_bps,
        min_lead_ratio=args.min_lead_ratio,
        confirm_updates=args.confirm_updates,
        confirm_ms=args.confirm_ms,
        rearm_fraction=args.rearm_fraction,
    )

    wake = asyncio.Event()
    binance = EventBinanceBookTickerFeed(contracts, models, wake)
    mexc = EventMexcDepthFeed(symbols, models, wake, depth_limit=args.depth_limit)
    await binance.start()
    await mexc.start()
    fee_cache = FeeCache()
    fee_stop = asyncio.Event()
    fee_task = asyncio.create_task(_fee_loop(fee_cache, fee_stop))

    pending: PendingEntry | None = None
    pos: Position | None = None
    stats = Stats()
    trades: list[TradeRow] = []
    deadline = time.monotonic() + args.session_seconds
    warmup_until = time.monotonic() + args.warmup_seconds
    next_latency_status = 0.0
    last_report: tuple | None = None
    output = Path(args.output)
    draining_announced = False

    try:
        while True:
            now = time.monotonic()
            now_ms = int(time.time() * 1000)
            budget_open = _run_budget_open(now, deadline, stats, args, trades)
            if not budget_open and not _must_drain(pending, pos):
                break
            if not budget_open and not draining_announced:
                draining_announced = True
                console.print(
                    "[yellow]DRAINING[/yellow] new signals disabled; any accepted pending entry/open position "
                    "will be carried to a terminal close before FINAL REPORT."
                )

            if now >= next_latency_status:
                console.print("LATENCY " + latency.status())
                next_latency_status = now + args.latency_status_seconds

            if pending is not None and pos is None and now >= pending.execute_at:
                sig = pending.signal
                entry_latency_ms = pending.latency_ms
                entry_latency_age_ms = pending.latency_age_ms
                replay_exit_ms = pending.replay_exit_ms
                scheduled_entry_ms = pending.scheduled_arrival_ms
                pending = None
                current = mexc.books.get(sig.symbol)
                snap = models[sig.symbol].snapshot(now_ms=now_ms, threshold_bps=0.0)
                if (
                    current is None or now_ms - current.recv_ms > args.max_book_age_ms
                    or not fee_cache.fresh_zero(sig.symbol, now_ms) or not v2._valid_snapshot(snap)
                ):
                    stats.expired += 1
                else:
                    ok, why, residual_ret, impulse_ret = delayed_catchup_entry_ok(
                        signal=sig,
                        current_residual_bps=snap.edge_bps,
                        current_binance_price=snap.binance_mid,
                        current_spread_bps=current.spread_bps,
                        min_residual_retention=args.min_residual_retention,
                        min_impulse_retention=args.min_impulse_retention,
                        min_remaining_edge_bps=args.min_absolute_residual_bps,
                        min_edge_after_spread_bps=args.min_edge_after_spread_bps,
                    )
                    if not ok:
                        stats.expired += 1
                        console.print(
                            f"EXPIRED {sig.symbol} reason={why} entry_latency={entry_latency_ms:.1f}ms "
                            f"residual_ret={residual_ret:.1%} impulse_ret={impulse_ret:.1%}"
                        )
                    else:
                        contract = by_symbol[sig.symbol]
                        fill = virtual_ioc_fill(
                            current,
                            direction=sig.direction,
                            target_notional_usdt=args.target_notional_usdt,
                            contract_size=contract.contract_size,
                            cross_bps=args.ioc_cross_bps,
                        )
                        notional = fill.qty * fill.avg_price
                        slip = v2.entry_slippage_bps(sig.direction, current, fill.avg_price)
                        if fill.qty <= 0 or notional < args.min_filled_notional_usdt:
                            stats.no_fill += 1
                        elif slip > args.max_entry_slippage_bps + 1e-9:
                            stats.expired += 1
                        else:
                            cost = immediate_roundtrip_cost_bps(
                                current,
                                direction=sig.direction,
                                entry_price=fill.avg_price,
                                qty=fill.qty,
                                contract_size=contract.contract_size,
                            )
                            edge_ok, required = v2.executable_edge_ok(
                                snap.edge_bps,
                                cost,
                                args.min_executable_net_edge_bps,
                                args.min_edge_to_cost_ratio,
                            )
                            if not edge_ok:
                                stats.expired += 1
                                console.print(
                                    f"EXPIRED {sig.symbol} reason=arrival_edge_below_cost residual={abs(snap.edge_bps):.2f}bps "
                                    f"cost={cost:.2f} required={required:.2f} entry_latency={entry_latency_ms:.1f}ms"
                                )
                            else:
                                stats.entries += 1
                                stats.fills.append(fill.fill_ratio)
                                stats.notionals.append(notional)
                                pos = Position(
                                    signal=sig,
                                    entry_ts_ms=now_ms,
                                    entry_scheduled_arrival_ms=scheduled_entry_ms,
                                    original_qty=fill.qty,
                                    remaining_qty=fill.qty,
                                    entry_price=fill.avg_price,
                                    entry_mid=current.mid,
                                    entry_binance_price=snap.binance_mid,
                                    entry_residual_bps=snap.edge_bps,
                                    entry_notional=notional,
                                    entry_fill_ratio=fill.fill_ratio,
                                    entry_latency_ms=entry_latency_ms,
                                    entry_latency_age_ms=entry_latency_age_ms,
                                    replay_exit_ms=replay_exit_ms,
                                    trailing=PositiveTrailing(
                                        distance_bps=max(args.trailing_distance_bps, current.spread_bps)
                                    ),
                                )
                                p = profile_by_symbol[sig.symbol]
                                overrun = max(0, now_ms - scheduled_entry_ms)
                                console.print(
                                    f"[green]ENTRY ARRIVED[/green] {sig.symbol} {'LONG' if sig.direction > 0 else 'SHORT'} "
                                    f"requested=${args.target_notional_usdt:.0f} filled=${notional:.0f} ({fill.fill_ratio:.1%}) "
                                    f"measured_entry_latency={entry_latency_ms:.1f}ms schedule_overrun={overrun}ms "
                                    f"sample_age={entry_latency_age_ms:.0f}ms residual={snap.edge_bps:+.2f}bps cost={cost:.2f}bps "
                                    f"lag_p75/p90={p.p75_lifetime_ms:.0f}/{p.p90_lifetime_ms:.0f}ms"
                                )

            if pos is not None:
                book = mexc.books.get(pos.signal.symbol)

                if pos.exit_pending is not None:
                    pending_exit = pos.exit_pending
                    if now >= pending_exit.execute_at:
                        if not pending_exit.arrived:
                            pending_exit.arrived = True
                            pending_exit.actual_arrival_ms = now_ms
                            overrun = max(0, now_ms - pending_exit.scheduled_arrival_ms)
                            console.print(
                                f"EXIT ARRIVED {pos.signal.symbol} reason={pending_exit.reason} "
                                f"decision_to_arrival={now_ms-pending_exit.decision_ms}ms "
                                f"schedule_overrun={overrun}ms"
                            )
                        if book is not None:
                            chunk_qty, exit_vwap = _exit_depth_for_qty(
                                book,
                                direction=pos.signal.direction,
                                qty=pos.remaining_qty,
                                contract_size=by_symbol[pos.signal.symbol].contract_size,
                            )
                            if chunk_qty > 0 and exit_vwap > 0:
                                chunk_pnl = pos.signal.direction * chunk_qty * (exit_vwap - pos.entry_price)
                                pos.realized_pnl_usdt += chunk_pnl
                                pos.remaining_qty = max(0.0, pos.remaining_qty - chunk_qty)
                                if pos.remaining_qty <= 1e-12:
                                    actual_arrival_ms = pending_exit.actual_arrival_ms or now_ms
                                    pnl_bps = pos.realized_pnl_usdt / max(pos.entry_notional, 1e-12) * 10_000.0
                                    row = TradeRow(
                                        symbol=pos.signal.symbol,
                                        direction=pos.signal.direction,
                                        signal_ms=pos.signal.ts_ms,
                                        entry_scheduled_arrival_ms=pos.entry_scheduled_arrival_ms,
                                        entry_arrival_ms=pos.entry_ts_ms,
                                        entry_schedule_overrun_ms=max(0, pos.entry_ts_ms-pos.entry_scheduled_arrival_ms),
                                        exit_decision_ms=pending_exit.decision_ms,
                                        exit_scheduled_arrival_ms=pending_exit.scheduled_arrival_ms,
                                        exit_arrival_ms=actual_arrival_ms,
                                        exit_schedule_overrun_ms=max(0, actual_arrival_ms-pending_exit.scheduled_arrival_ms),
                                        close_ms=now_ms,
                                        exit_book_wait_ms=max(0, now_ms-actual_arrival_ms),
                                        measured_entry_latency_ms=pos.entry_latency_ms,
                                        entry_latency_age_ms=pos.entry_latency_age_ms,
                                        measured_exit_latency_ms=pending_exit.latency_ms,
                                        exit_latency_age_ms=pending_exit.latency_age_ms,
                                        exit_latency_stale_fallback=pending_exit.stale_fallback,
                                        entry_price=pos.entry_price,
                                        exit_vwap=exit_vwap,
                                        requested_notional_usdt=args.target_notional_usdt,
                                        filled_notional_usdt=pos.entry_notional,
                                        fill_ratio=pos.entry_fill_ratio,
                                        pnl_usdt=pos.realized_pnl_usdt,
                                        pnl_bps=pnl_bps,
                                        hold_from_entry_ms=now_ms - pos.entry_ts_ms,
                                        signal_to_close_ms=now_ms - pos.signal.ts_ms,
                                        exit_reason=pending_exit.reason,
                                    )
                                    trades.append(row)
                                    _record_close(stats, row)
                                    _write_rows(output, trades)
                                    console.print(
                                        f"[{'green' if row.pnl_usdt > 0 else 'red'}]EXIT FILLED[/] {row.symbol} {row.exit_reason} "
                                        f"pnl={row.pnl_bps:+.2f}bps ${row.pnl_usdt:+.2f} "
                                        f"entry_lat={row.measured_entry_latency_ms:.1f}ms "
                                        f"exit_lat={row.measured_exit_latency_ms:.1f}ms "
                                        f"exit_overrun={row.exit_schedule_overrun_ms}ms "
                                        f"book_wait={row.exit_book_wait_ms}ms hold={row.hold_from_entry_ms}ms "
                                        f"signal_to_close={row.signal_to_close_ms}ms"
                                    )
                                    pos = None

                elif book is not None:
                    snap = models[pos.signal.symbol].snapshot(now_ms=now_ms, threshold_bps=0.0)
                    if v2._valid_snapshot(snap):
                        age_ms = now_ms - pos.entry_ts_ms
                        mid_move = directional_move_bps(pos.signal.direction, pos.entry_mid, book.mid)
                        leader_move = directional_move_bps(
                            pos.signal.direction, pos.entry_binance_price, snap.binance_mid
                        )
                        conv = max(args.convergence_bps, abs(pos.entry_residual_bps) * args.convergence_fraction)
                        residual_dir = 1 if snap.edge_bps > 0 else -1 if snap.edge_bps < 0 else 0
                        full_filled, full_exit = _exit_depth_for_qty(
                            book,
                            direction=pos.signal.direction,
                            qty=pos.remaining_qty,
                            contract_size=by_symbol[pos.signal.symbol].contract_size,
                        )
                        executable_pnl_bps = (
                            _signed_move_bps(pos.signal.direction, pos.entry_price, full_exit)
                            if full_filled + 1e-12 >= pos.remaining_qty and full_exit > 0
                            else None
                        )
                        trail = pos.trailing.update(executable_pnl_bps) if executable_pnl_bps is not None else None
                        reason: str | None = None
                        if age_ms >= args.min_hold_ms:
                            if mid_move <= -args.mid_adverse_cut_bps:
                                reason = "mid_adverse_cut"
                            elif leader_move <= -args.leader_retrace_exit_bps:
                                reason = "leader_retrace"
                            elif residual_dir == -pos.signal.direction and abs(snap.edge_bps) >= args.reversal_edge_bps:
                                reason = "residual_reversal"
                            elif abs(snap.edge_bps) <= conv and mid_move >= args.min_catchup_bps:
                                reason = "mexc_catchup_convergence"
                            elif age_ms >= args.no_progress_ms and mid_move < args.min_progress_bps:
                                reason = "no_progress"
                            elif trail is not None and executable_pnl_bps is not None and executable_pnl_bps <= trail:
                                reason = "positive_trailing_stop"
                            elif age_ms >= args.max_hold_ms:
                                reason = "timeout"

                        if reason is not None:
                            exit_latency = latency.exit(pos.replay_exit_ms)
                            if exit_latency is None:
                                exit_latency = ExitLatency(
                                    value_ms=pos.entry_latency_ms,
                                    age_ms=pos.entry_latency_age_ms,
                                    stale_fallback=True,
                                )
                            scheduled_arrival_ms = now_ms + int(round(exit_latency.value_ms))
                            pos.exit_pending = PendingExit(
                                reason=reason,
                                decision_ms=now_ms,
                                execute_at=now + exit_latency.value_ms / 1000.0,
                                scheduled_arrival_ms=scheduled_arrival_ms,
                                latency_ms=exit_latency.value_ms,
                                latency_age_ms=exit_latency.age_ms,
                                stale_fallback=exit_latency.stale_fallback,
                            )
                            console.print(
                                f"EXIT DECISION {pos.signal.symbol} reason={reason} decision_hold={age_ms}ms "
                                f"measured_exit_latency={exit_latency.value_ms:.1f}ms "
                                f"scheduled_arrival={scheduled_arrival_ms} sample_age={exit_latency.age_ms:.0f}ms "
                                f"stale_fallback={exit_latency.stale_fallback}"
                            )

            if budget_open and now >= warmup_until and pending is None and pos is None:
                candidates = []
                for symbol in symbols:
                    if not fee_cache.fresh_zero(symbol, now_ms):
                        continue
                    book = mexc.books.get(symbol)
                    if book is None or now_ms - book.recv_ms > args.max_book_age_ms:
                        continue
                    model = models[symbol]
                    snap = model.snapshot(now_ms=now_ms, threshold_bps=0.0)
                    if not v2._valid_snapshot(snap):
                        continue
                    d = gate.observe(symbol, snap, book.spread_bps, now_ms, event_key=v2._event_key(model))
                    if not d.ready:
                        continue
                    strength = abs(d.residual_bps) / max(d.threshold_bps, 1e-12)
                    if strength < args.min_signal_strength_ratio or abs(d.residual_bps) < args.min_absolute_residual_bps:
                        continue
                    candidates.append((abs(d.residual_bps), strength, d.leader_advantage_bps, symbol, d, snap, book))

                if candidates:
                    _, strength, _, symbol, d, snap, book = max(
                        candidates, key=lambda row: (row[0], row[1], row[2])
                    )
                    entry_latency = latency.entry()
                    if entry_latency is None:
                        stats.latency_rejects += 1
                        console.print(
                            f"[yellow]LATENCY BLOCK[/yellow] {symbol}: no fresh realtime sample; {latency.status()}"
                        )
                    else:
                        stats.signals += 1
                        sig = Signal(
                            signal_id=f"e2e-{stats.signals}-{now_ms}",
                            ts_ms=now_ms,
                            symbol=symbol,
                            direction=d.direction,
                            residual_bps=d.residual_bps,
                            threshold_bps=d.threshold_bps,
                            noise_bps=d.noise_bps,
                            spread_bps=book.spread_bps,
                            leader_advantage_bps=d.leader_advantage_bps,
                            binance_move_bps=d.binance_move_bps,
                            mexc_move_bps=d.mexc_move_bps,
                            binance_price=snap.binance_mid,
                            mexc_price=snap.mexc_mid,
                        )
                        scheduled_arrival_ms = now_ms + int(round(entry_latency.value_ms))
                        pending = PendingEntry(
                            signal=sig,
                            execute_at=now + entry_latency.value_ms / 1000.0,
                            scheduled_arrival_ms=scheduled_arrival_ms,
                            latency_ms=entry_latency.value_ms,
                            latency_age_ms=entry_latency.age_ms,
                            replay_exit_ms=entry_latency.replay_exit_ms,
                        )
                        console.print(
                            f"SIGNAL #{stats.signals} {symbol} {'LONG' if d.direction > 0 else 'SHORT'} "
                            f"residual={d.residual_bps:+.2f}bps strength={strength:.2f}x spread={book.spread_bps:.2f}bps "
                            f"measured_entry_latency={entry_latency.value_ms:.1f}ms "
                            f"scheduled_arrival={scheduled_arrival_ms} sample_age={entry_latency.age_ms:.0f}ms"
                        )

            report = (
                stats.signals, stats.entries, stats.expired, stats.no_fill,
                stats.latency_rejects, stats.wins, stats.losses, stats.flats,
                round(stats.pnl_usdt, 6),
            )
            if report != last_report:
                console.print("STATE " + _summary(stats))
                last_report = report

            wake.clear()
            try:
                await asyncio.wait_for(wake.wait(), timeout=0.01)
            except TimeoutError:
                pass
    finally:
        fee_stop.set()
        fee_task.cancel()
        try:
            await fee_task
        except asyncio.CancelledError:
            pass
        await binance.close()
        await mexc.close()
        await latency.close()
        _write_rows(output, trades)

    console.print("\n[bold]FINAL PERSISTENT END-TO-END REPORT[/bold]")
    console.print(_summary(stats))
    console.print(f"CSV: {output.resolve()}")
    console.print("READ-ONLY CONFIRMED: no LIVE or Testnet order write is constructed by this runner.")
    return trades


def build_parser() -> argparse.ArgumentParser:
    p = v2.build_parser()
    p.description = "Frozen persistent Binance->LIVE MEXC alpha with realtime measured entry/exit latency"
    p.add_argument("--target-closed-trades", type=int, default=100)
    p.add_argument(
        "--latency-csv",
        default="",
        help="explicit historical replay only; when omitted latency is measured continuously in realtime",
    )
    p.add_argument("--latency-profile", choices=("latest", "median", "p75", "p95"), default="p75")
    p.add_argument("--latency-probe-interval-ms", type=float, default=250.0)
    p.add_argument("--latency-window", type=int, default=31)
    p.add_argument("--latency-min-samples", type=int, default=5)
    p.add_argument("--latency-max-age-seconds", type=float, default=2.0)
    p.add_argument("--latency-status-seconds", type=float, default=5.0)
    p.add_argument("--output", default="persistent_end2end_latency.csv")
    return p


def main() -> None:
    args = build_parser().parse_args()
    apply_baseline_v1(args)
    if args.target_closed_trades <= 0:
        raise SystemExit("--target-closed-trades must be positive")
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
