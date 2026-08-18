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
from .canonical_latency import RealtimeExecutionLatency
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

console = Console()
REFERENCE_COMMIT = "8a0bc6043385dbaf95ec8e77b93d91fd00a7f9e5"


@dataclass(slots=True)
class PendingEntry:
    signal: Signal
    execute_at: float
    scheduled_ms: float
    sample_age_ms: float


@dataclass(slots=True)
class PendingExit:
    reason: str
    decision_ms: int
    execute_at: float
    scheduled_ms: float
    sample_age_ms: float
    actual_arrival_ms: int | None = None


@dataclass(slots=True)
class Position:
    signal: Signal
    entry_arrival_ms: int
    entry_price: float
    entry_mid: float
    entry_binance_price: float
    entry_residual_bps: float
    entry_notional: float
    fill_ratio: float
    qty: float
    remaining_qty: float
    scheduled_entry_latency_ms: float
    actual_entry_latency_ms: int
    trailing: PositiveTrailing
    exit_pending: PendingExit | None = None
    realized_pnl_usdt: float = 0.0
    last_exit_book_recv_ms: int = 0


@dataclass(slots=True)
class TradeRow:
    symbol: str
    direction: str
    signal_ms: int
    entry_arrival_ms: int
    scheduled_entry_latency_ms: float
    actual_entry_latency_ms: int
    exit_decision_ms: int
    exit_arrival_ms: int
    scheduled_exit_latency_ms: float
    actual_exit_latency_ms: int
    exit_schedule_overrun_ms: int
    close_ms: int
    requested_notional_usdt: float
    filled_notional_usdt: float
    fill_ratio: float
    pnl_usdt: float
    pnl_bps: float
    hold_ms: int
    signal_to_close_ms: int
    exit_reason: str


@dataclass(slots=True)
class Stats:
    signals: int = 0
    entries: int = 0
    expired: int = 0
    nofill: int = 0
    latency_blocks: int = 0
    wins: int = 0
    losses: int = 0
    flats: int = 0
    pnl_usdt: float = 0.0
    gross_win: float = 0.0
    gross_loss: float = 0.0
    fills: list[float] = field(default_factory=list)
    notionals: list[float] = field(default_factory=list)
    holds: list[float] = field(default_factory=list)
    total_ms: list[float] = field(default_factory=list)
    reasons: dict[str, int] = field(default_factory=dict)

    @property
    def pf(self) -> float:
        if self.gross_loss <= 0:
            return math.inf if self.gross_win > 0 else 0.0
        return self.gross_win / self.gross_loss


def _summary(s: Stats) -> str:
    closed = s.wins + s.losses + s.flats
    wr = 100.0 * s.wins / closed if closed else 0.0
    pf = "inf" if math.isinf(s.pf) else f"{s.pf:.3f}"
    fill = statistics.median(s.fills) * 100 if s.fills else 0.0
    notional = statistics.median(s.notionals) if s.notionals else 0.0
    hold = statistics.median(s.holds) if s.holds else 0.0
    total = statistics.median(s.total_ms) if s.total_ms else 0.0
    reasons = ",".join(f"{k}:{v}" for k, v in sorted(s.reasons.items())) or "-"
    return (
        f"signals={s.signals} entries={s.entries} expired={s.expired} nofill={s.nofill} "
        f"latency_blocks={s.latency_blocks} W/L/F={s.wins}/{s.losses}/{s.flats} WR={wr:.1f}% "
        f"PF_USDT={pf} pnl={s.pnl_usdt:+.4f}USDT fill_med={fill:.1f}% notional_med=${notional:.0f} "
        f"hold_med={hold:.0f}ms signal_to_close_med={total:.0f}ms exits={reasons}"
    )


def _write_csv(path: Path, rows: list[TradeRow]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(TradeRow.__dataclass_fields__))
        writer.writeheader()
        for row in rows:
            writer.writerow({name: getattr(row, name) for name in TradeRow.__dataclass_fields__})


def _record(stats: Stats, row: TradeRow) -> None:
    stats.pnl_usdt += row.pnl_usdt
    stats.holds.append(float(row.hold_ms))
    stats.total_ms.append(float(row.signal_to_close_ms))
    stats.reasons[row.exit_reason] = stats.reasons.get(row.exit_reason, 0) + 1
    if row.pnl_usdt > 1e-9:
        stats.wins += 1
        stats.gross_win += row.pnl_usdt
    elif row.pnl_usdt < -1e-9:
        stats.losses += 1
        stats.gross_loss += abs(row.pnl_usdt)
    else:
        stats.flats += 1


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
    contracts = [x for x in await discover_live_zero_fee_crosslisted() if x.mexc_symbol in profile_by_symbol]
    if not contracts:
        raise RuntimeError("No frozen persistent pair is currently exact-0/0 and Binance-crosslisted")

    latency = RealtimeExecutionLatency(
        interval_ms=args.latency_probe_interval_ms,
        window=args.latency_window,
        minimum_samples=args.latency_min_samples,
    )
    await latency.start()

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
        )
        for x in contracts
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
    accepting_new = True
    last_state: tuple | None = None
    output = Path(args.output)

    console.print("[bold cyan]CANONICAL PERSISTENT LATENCY-ARB SHADOW[/bold cyan] - READ ONLY")
    console.print(f"Frozen alpha={REFERENCE_COMMIT}; lifetime={source.resolve()}")
    console.print("No fixed latency constants. New entries require fresh current latency; exits never wait for Binance after decision.")

    try:
        while True:
            now = time.monotonic()
            now_ms = int(time.time() * 1000)

            if accepting_new and (
                now >= deadline or stats.signals >= args.max_signals or len(trades) >= args.target_closed_trades
            ):
                accepting_new = False
                if pending is not None:
                    pending = None
                    stats.expired += 1
                console.print("DRAIN MODE: no new signals; finishing any open position before final report.")

            if not accepting_new and pos is None and pending is None:
                break

            # ENTRY ARRIVAL: full frozen arrival-book recheck.
            if pending is not None and pos is None and now >= pending.execute_at:
                sig = pending.signal
                scheduled = pending.scheduled_ms
                actual_entry_latency = max(0, now_ms - sig.ts_ms)
                pending = None
                current = mexc.books.get(sig.symbol)
                snap = models[sig.symbol].snapshot(now_ms=now_ms, threshold_bps=0.0)
                if current is None or now_ms - current.recv_ms > args.max_book_age_ms or not fee_cache.fresh_zero(sig.symbol, now_ms) or not v2._valid_snapshot(snap):
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
                        console.print(f"EXPIRED {sig.symbol} reason={why} residual_ret={residual_ret:.1%} impulse_ret={impulse_ret:.1%}")
                    else:
                        contract = by_symbol[sig.symbol]
                        fill = virtual_ioc_fill(current, direction=sig.direction, target_notional_usdt=args.target_notional_usdt, contract_size=contract.contract_size, cross_bps=args.ioc_cross_bps)
                        notional = fill.qty * fill.avg_price
                        slip = v2.entry_slippage_bps(sig.direction, current, fill.avg_price)
                        if fill.qty <= 0 or notional < args.min_filled_notional_usdt:
                            stats.nofill += 1
                        elif slip > args.max_entry_slippage_bps + 1e-9:
                            stats.expired += 1
                        else:
                            cost = immediate_roundtrip_cost_bps(current, direction=sig.direction, entry_price=fill.avg_price, qty=fill.qty, contract_size=contract.contract_size)
                            edge_ok, required = v2.executable_edge_ok(snap.edge_bps, cost, args.min_executable_net_edge_bps, args.min_edge_to_cost_ratio)
                            if not edge_ok:
                                stats.expired += 1
                                console.print(f"EXPIRED {sig.symbol} reason=arrival_edge_below_cost residual={abs(snap.edge_bps):.2f}bps cost={cost:.2f} required={required:.2f}")
                            else:
                                stats.entries += 1
                                stats.fills.append(fill.fill_ratio)
                                stats.notionals.append(notional)
                                pos = Position(
                                    signal=sig,
                                    entry_arrival_ms=now_ms,
                                    entry_price=fill.avg_price,
                                    entry_mid=current.mid,
                                    entry_binance_price=snap.binance_mid,
                                    entry_residual_bps=snap.edge_bps,
                                    entry_notional=notional,
                                    fill_ratio=fill.fill_ratio,
                                    qty=fill.qty,
                                    remaining_qty=fill.qty,
                                    scheduled_entry_latency_ms=scheduled,
                                    actual_entry_latency_ms=actual_entry_latency,
                                    trailing=PositiveTrailing(distance_bps=max(args.trailing_distance_bps, current.spread_bps)),
                                )
                                p = profile_by_symbol[sig.symbol]
                                console.print(
                                    f"[green]ENTRY ARRIVED[/green] {sig.symbol} {'LONG' if sig.direction > 0 else 'SHORT'} "
                                    f"filled=${notional:.0f} ({fill.fill_ratio:.1%}) scheduled_lat={scheduled:.1f}ms actual_lat={actual_entry_latency}ms "
                                    f"residual={snap.edge_bps:+.2f} cost={cost:.2f} lag_p75/p90={p.p75_lifetime_ms:.0f}/{p.p90_lifetime_ms:.0f}ms"
                                )

            if pos is not None:
                book = mexc.books.get(pos.signal.symbol)

                # BEFORE an exit decision, frozen semantics still use Binance+MEXC state.
                if pos.exit_pending is None and book is not None:
                    snap = models[pos.signal.symbol].snapshot(now_ms=now_ms, threshold_bps=0.0)
                    if v2._valid_snapshot(snap):
                        age_ms = now_ms - pos.entry_arrival_ms
                        mid_move = directional_move_bps(pos.signal.direction, pos.entry_mid, book.mid)
                        leader_move = directional_move_bps(pos.signal.direction, pos.entry_binance_price, snap.binance_mid)
                        conv = max(args.convergence_bps, abs(pos.entry_residual_bps) * args.convergence_fraction)
                        residual_dir = 1 if snap.edge_bps > 0 else -1 if snap.edge_bps < 0 else 0
                        full_qty, full_exit = _exit_depth_for_qty(book, direction=pos.signal.direction, qty=pos.remaining_qty, contract_size=by_symbol[pos.signal.symbol].contract_size)
                        executable_pnl = _signed_move_bps(pos.signal.direction, pos.entry_price, full_exit) if full_qty + 1e-12 >= pos.remaining_qty and full_exit > 0 else None
                        trail = pos.trailing.update(executable_pnl) if executable_pnl is not None else None
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
                            elif trail is not None and executable_pnl is not None and executable_pnl <= trail:
                                reason = "positive_trailing_stop"
                            elif age_ms >= args.max_hold_ms:
                                reason = "timeout"
                        if reason is not None:
                            exit_probe = latency.best_effort_exit_ms()
                            scheduled = pos.scheduled_entry_latency_ms if exit_probe is None else exit_probe[0]
                            age = 0.0 if exit_probe is None else exit_probe[1]
                            pos.exit_pending = PendingExit(reason=reason, decision_ms=now_ms, execute_at=now + scheduled / 1000.0, scheduled_ms=scheduled, sample_age_ms=age)
                            console.print(f"EXIT DECISION {pos.signal.symbol} reason={reason} scheduled_exit_latency={scheduled:.1f}ms sample_age={age:.0f}ms")

                # AFTER an exit decision, Binance is irrelevant. Only elapsed latency + fresh MEXC depth matter.
                if pos.exit_pending is not None and now >= pos.exit_pending.execute_at:
                    pe = pos.exit_pending
                    if pe.actual_arrival_ms is None:
                        pe.actual_arrival_ms = now_ms
                        actual = now_ms - pe.decision_ms
                        overrun = actual - int(round(pe.scheduled_ms))
                        console.print(f"EXIT ARRIVED {pos.signal.symbol} reason={pe.reason} actual={actual}ms overrun={overrun:+d}ms")
                    book = mexc.books.get(pos.signal.symbol)
                    if book is not None and now_ms - book.recv_ms <= args.max_book_age_ms and book.recv_ms != pos.last_exit_book_recv_ms:
                        pos.last_exit_book_recv_ms = book.recv_ms
                        chunk_qty, exit_vwap = _exit_depth_for_qty(book, direction=pos.signal.direction, qty=pos.remaining_qty, contract_size=by_symbol[pos.signal.symbol].contract_size)
                        if chunk_qty > 0 and exit_vwap > 0:
                            pos.realized_pnl_usdt += pos.signal.direction * chunk_qty * (exit_vwap - pos.entry_price)
                            pos.remaining_qty = max(0.0, pos.remaining_qty - chunk_qty)
                            if pos.remaining_qty <= 1e-12:
                                actual_exit_latency = max(0, (pe.actual_arrival_ms or now_ms) - pe.decision_ms)
                                pnl_bps = pos.realized_pnl_usdt / max(pos.entry_notional, 1e-12) * 10_000.0
                                row = TradeRow(
                                    symbol=pos.signal.symbol,
                                    direction="LONG" if pos.signal.direction > 0 else "SHORT",
                                    signal_ms=pos.signal.ts_ms,
                                    entry_arrival_ms=pos.entry_arrival_ms,
                                    scheduled_entry_latency_ms=pos.scheduled_entry_latency_ms,
                                    actual_entry_latency_ms=pos.actual_entry_latency_ms,
                                    exit_decision_ms=pe.decision_ms,
                                    exit_arrival_ms=pe.actual_arrival_ms or now_ms,
                                    scheduled_exit_latency_ms=pe.scheduled_ms,
                                    actual_exit_latency_ms=actual_exit_latency,
                                    exit_schedule_overrun_ms=actual_exit_latency - int(round(pe.scheduled_ms)),
                                    close_ms=now_ms,
                                    requested_notional_usdt=args.target_notional_usdt,
                                    filled_notional_usdt=pos.entry_notional,
                                    fill_ratio=pos.fill_ratio,
                                    pnl_usdt=pos.realized_pnl_usdt,
                                    pnl_bps=pnl_bps,
                                    hold_ms=now_ms - pos.entry_arrival_ms,
                                    signal_to_close_ms=now_ms - pos.signal.ts_ms,
                                    exit_reason=pe.reason,
                                )
                                trades.append(row)
                                _record(stats, row)
                                _write_csv(output, trades)
                                console.print(f"[{'green' if row.pnl_usdt > 0 else 'red'}]EXIT FILLED[/] {row.symbol} {row.exit_reason} pnl={row.pnl_bps:+.2f}bps ${row.pnl_usdt:+.2f} hold={row.hold_ms}ms")
                                pos = None

            if accepting_new and now >= warmup_until and pending is None and pos is None:
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
                    if strength >= args.min_signal_strength_ratio and abs(d.residual_bps) >= args.min_absolute_residual_bps:
                        candidates.append((abs(d.residual_bps), strength, d.leader_advantage_bps, symbol, d, snap, book))
                if candidates:
                    _, strength, _, symbol, d, snap, book = max(candidates, key=lambda row: (row[0], row[1], row[2]))
                    measured = latency.fresh_effective_ms(max_age_ms=args.latency_max_age_ms)
                    if measured is None:
                        stats.latency_blocks += 1
                    else:
                        scheduled, sample_age = measured
                        stats.signals += 1
                        sig = Signal(
                            signal_id=f"canonical-{stats.signals}-{now_ms}", ts_ms=now_ms, symbol=symbol,
                            direction=d.direction, residual_bps=d.residual_bps, threshold_bps=d.threshold_bps,
                            noise_bps=d.noise_bps, spread_bps=book.spread_bps, leader_advantage_bps=d.leader_advantage_bps,
                            binance_move_bps=d.binance_move_bps, mexc_move_bps=d.mexc_move_bps,
                            binance_price=snap.binance_mid, mexc_price=snap.mexc_mid,
                        )
                        pending = PendingEntry(signal=sig, execute_at=now + scheduled / 1000.0, scheduled_ms=scheduled, sample_age_ms=sample_age)
                        console.print(f"SIGNAL #{stats.signals} {symbol} {'LONG' if d.direction > 0 else 'SHORT'} residual={d.residual_bps:+.2f}bps strength={strength:.2f}x scheduled_entry_latency={scheduled:.1f}ms")

            state = (stats.signals, stats.entries, stats.expired, stats.nofill, stats.latency_blocks, stats.wins, stats.losses, stats.flats, round(stats.pnl_usdt, 6))
            if state != last_state:
                console.print("STATE " + _summary(stats))
                last_state = state

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
        _write_csv(output, trades)

    console.print("\n[bold]FINAL CANONICAL END-TO-END REPORT[/bold]")
    console.print(_summary(stats))
    console.print(f"CSV: {output.resolve()}")
    return trades


def build_parser() -> argparse.ArgumentParser:
    p = v2.build_parser()
    p.description = "Canonical frozen persistent Binance->LIVE MEXC end-to-end latency shadow"
    p.add_argument("--target-closed-trades", type=int, default=100)
    p.add_argument("--latency-probe-interval-ms", type=float, default=250.0)
    p.add_argument("--latency-window", type=int, default=31)
    p.add_argument("--latency-min-samples", type=int, default=5)
    p.add_argument("--latency-max-age-ms", type=float, default=2000.0)
    p.add_argument("--output", default="canonical_end2end.csv")
    return p


def main() -> None:
    args = build_parser().parse_args()
    apply_baseline_v1(args)
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
