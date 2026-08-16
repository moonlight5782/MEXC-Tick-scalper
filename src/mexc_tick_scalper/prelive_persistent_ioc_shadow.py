from __future__ import annotations

import argparse
import asyncio
import math
import statistics
import time
from dataclasses import dataclass, field
from pathlib import Path

from rich.console import Console

from .lead_lag_strategy import LeadLagGate
from .live_lead_lag_shadow import PositiveTrailing
from .live_production_runner import FeeCache, _fee_loop, _signed_move_bps
from .live_zero_fee_universe import LiveZeroFeeContract, discover_live_zero_fee_crosslisted
from .microspread import MicroSpreadModel
from .microspread_feed import EventBinanceBookTickerFeed, EventMexcDepthFeed, LiveBook
from .persistent_lag_profile import build_profiles, latest_lifetime_csv, select_profiles
from .prelive_latency_diagnostic import _exit_depth_for_qty
from .prelive_measured_rtt_diagnostic import measure_live_private_rtt, _percentile
from .prelive_persistent_catchup_shadow import (
    Signal,
    delayed_catchup_entry_ok,
    directional_move_bps,
)

console = Console()


@dataclass(frozen=True, slots=True)
class IocFill:
    qty: float
    avg_price: float
    requested_qty: float
    limit_price: float

    @property
    def fill_ratio(self) -> float:
        return min(1.0, self.qty / self.requested_qty) if self.requested_qty > 0 else 0.0


@dataclass(slots=True)
class Pending:
    signal: Signal
    execute_at: float


@dataclass(slots=True)
class Position:
    signal: Signal
    entry_ts_ms: int
    qty: float
    entry_price: float
    entry_mid: float
    entry_binance_price: float
    entry_residual_bps: float
    entry_spread_bps: float
    entry_fill_ratio: float
    entry_notional: float
    trailing: PositiveTrailing
    mfe_bps: float = 0.0
    mae_bps: float = 0.0


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
    gross_win_bps: float = 0.0
    gross_loss_bps: float = 0.0
    fills: list[float] = field(default_factory=list)
    notionals: list[float] = field(default_factory=list)
    holds: list[float] = field(default_factory=list)
    reasons: dict[str, int] = field(default_factory=dict)

    @property
    def pf(self) -> float:
        if self.gross_loss_bps <= 0:
            return math.inf if self.gross_win_bps > 0 else 0.0
        return self.gross_win_bps / self.gross_loss_bps


def _event_key(model: MicroSpreadModel) -> tuple[int, int] | None:
    if not model.binance or not model.mexc:
        return None
    return int(model.binance[-1][0]), int(model.mexc[-1][0])


def _valid_snapshot(snap) -> bool:
    return snap.reason not in {
        "warming_up", "warming_baseline", "warming_horizon", "stale_binance", "stale_mexc"
    }


def virtual_ioc_fill(
    book: LiveBook,
    *,
    direction: int,
    target_notional_usdt: float,
    contract_size: float,
    cross_bps: float,
) -> IocFill:
    """Simulate a marketable IOC LIMIT. Only price levels inside the IOC limit fill.

    Any unfilled remainder is cancelled. Quantities are returned in base units,
    matching the paper position/exit helpers.
    """
    if direction not in (-1, 1) or target_notional_usdt <= 0 or contract_size <= 0:
        return IocFill(0.0, 0.0, 0.0, 0.0)
    best = book.ask if direction > 0 else book.bid
    if best <= 0:
        return IocFill(0.0, 0.0, 0.0, 0.0)
    requested_qty = target_notional_usdt / best
    if direction > 0:
        limit_price = best * (1.0 + max(0.0, cross_bps) / 10_000.0)
        levels = book.asks
        allowed = lambda price: price <= limit_price + 1e-15
    else:
        limit_price = best * (1.0 - max(0.0, cross_bps) / 10_000.0)
        levels = book.bids
        allowed = lambda price: price >= limit_price - 1e-15

    remaining = requested_qty
    filled = 0.0
    quote = 0.0
    for price, contracts in levels:
        if price <= 0 or contracts <= 0 or not allowed(price):
            continue
        available = float(contracts) * float(contract_size)
        take = min(remaining, available)
        if take <= 0:
            continue
        filled += take
        quote += take * float(price)
        remaining -= take
        if remaining <= 1e-12:
            break
    return IocFill(filled, quote / filled if filled > 0 else 0.0, requested_qty, limit_price)


def immediate_roundtrip_cost_bps(
    book: LiveBook,
    *,
    direction: int,
    entry_price: float,
    qty: float,
    contract_size: float,
) -> float:
    """Executable cost of immediately flattening the actual IOC partial fill."""
    filled, exit_vwap = _exit_depth_for_qty(
        book, direction=direction, qty=qty, contract_size=contract_size
    )
    if filled + 1e-12 < qty or exit_vwap <= 0 or entry_price <= 0:
        return math.inf
    return max(0.0, -_signed_move_bps(direction, entry_price, exit_vwap))


def executable_entry_edge_ok(
    *,
    residual_bps: float,
    roundtrip_cost_bps: float,
    min_net_edge_bps: float,
    min_edge_to_cost_ratio: float,
) -> tuple[bool, float]:
    edge = abs(residual_bps)
    required = max(
        roundtrip_cost_bps + min_net_edge_bps,
        roundtrip_cost_bps * min_edge_to_cost_ratio,
    )
    return edge >= required, required


def _summary(s: Stats) -> str:
    closed = s.wins + s.losses + s.flats
    wr = s.wins / closed * 100 if closed else 0.0
    pf = "inf" if math.isinf(s.pf) else f"{s.pf:.3f}"
    fill = statistics.median(s.fills) * 100 if s.fills else 0.0
    notion = statistics.median(s.notionals) if s.notionals else 0.0
    hold = statistics.median(s.holds) if s.holds else 0.0
    reasons = ",".join(f"{k}:{v}" for k, v in sorted(s.reasons.items())) or "-"
    return (
        f"signals={s.signals} entries={s.entries} expired={s.expired} nofill={s.no_fill} "
        f"W/L/F={s.wins}/{s.losses}/{s.flats} WR={wr:.1f}% PF={pf} pnl={s.pnl_usdt:+.4f}USDT "
        f"fill_med={fill:.1f}% notional_med=${notion:.0f} hold_med={hold:.0f}ms exits={reasons}"
    )


async def run(args: argparse.Namespace) -> None:
    source = Path(args.lifetime_csv) if args.lifetime_csv else latest_lifetime_csv(Path.cwd())
    profiles = select_profiles(
        build_profiles(source),
        min_signals=args.pair_min_signals,
        min_median_lifetime_ms=args.pair_min_median_lifetime_ms,
        min_survival_rate=args.pair_min_survival_rate,
        min_signal_strength_ratio=args.pair_min_strength_ratio,
    )
    if not profiles:
        raise RuntimeError("No persistent-lag pair passed profile filters")
    keep = {p.symbol for p in profiles}
    contracts = [x for x in await discover_live_zero_fee_crosslisted() if x.mexc_symbol in keep]
    if not contracts:
        raise RuntimeError("No selected persistent pair is currently exact-0/0 and Binance-crosslisted")

    rtts = await measure_live_private_rtt(
        samples=args.rtt_samples,
        warmup_samples=args.rtt_warmup_samples,
        interval_ms=args.rtt_interval_ms,
    )
    rtt = statistics.median(rtts)
    console.print("[bold cyan]LIVE PAPER PARTIAL-IOC CATCH-UP[/bold cyan] - NO REAL ORDERS")
    console.print(f"Lifetime source: {source.resolve()}")
    console.print(
        f"Measured private RTT median={rtt:.1f}ms p95={_percentile(rtts,.95):.1f}ms; "
        f"IOC cross={args.ioc_cross_bps:.2f}bps target=${args.target_notional_usdt:g}"
    )
    console.print(
        f"Strong signal: residual>={args.min_absolute_residual_bps:.1f}bps strength>={args.min_signal_strength_ratio:.1f}x. "
        f"Executable edge must beat actual partial-fill roundtrip cost by +{args.min_executable_net_edge_bps:.1f}bps "
        f"and {args.min_edge_to_cost_ratio:.1f}x."
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

    pending: Pending | None = None
    pos: Position | None = None
    stats = Stats()
    deadline = time.monotonic() + args.session_seconds
    warmup_until = time.monotonic() + args.warmup_seconds
    next_heartbeat = 0.0

    try:
        while time.monotonic() < deadline and stats.signals < args.max_signals:
            now = time.monotonic()
            now_ms = int(time.time() * 1000)

            if pending is not None and pos is None and now >= pending.execute_at:
                sig = pending.signal
                pending = None
                book = mexc.books.get(sig.symbol)
                snap = models[sig.symbol].snapshot(now_ms=now_ms, threshold_bps=0.0)
                if (
                    book is None
                    or now_ms - book.recv_ms > args.max_book_age_ms
                    or not fee_cache.fresh_zero(sig.symbol, now_ms)
                    or not _valid_snapshot(snap)
                ):
                    stats.expired += 1
                else:
                    ok, _, _, _ = delayed_catchup_entry_ok(
                        signal=sig,
                        current_residual_bps=snap.edge_bps,
                        current_binance_price=snap.binance_mid,
                        current_spread_bps=book.spread_bps,
                        min_residual_retention=args.min_residual_retention,
                        min_impulse_retention=args.min_impulse_retention,
                        min_remaining_edge_bps=args.min_absolute_residual_bps,
                        min_edge_after_spread_bps=args.min_edge_after_spread_bps,
                    )
                    if not ok:
                        stats.expired += 1
                    else:
                        contract = by_symbol[sig.symbol]
                        fill = virtual_ioc_fill(
                            book,
                            direction=sig.direction,
                            target_notional_usdt=args.target_notional_usdt,
                            contract_size=contract.contract_size,
                            cross_bps=args.ioc_cross_bps,
                        )
                        notional = fill.qty * fill.avg_price
                        if fill.qty <= 0 or fill.avg_price <= 0 or notional < args.min_filled_notional_usdt:
                            stats.no_fill += 1
                        else:
                            rt_cost = immediate_roundtrip_cost_bps(
                                book,
                                direction=sig.direction,
                                entry_price=fill.avg_price,
                                qty=fill.qty,
                                contract_size=contract.contract_size,
                            )
                            edge_ok, required = executable_entry_edge_ok(
                                residual_bps=snap.edge_bps,
                                roundtrip_cost_bps=rt_cost,
                                min_net_edge_bps=args.min_executable_net_edge_bps,
                                min_edge_to_cost_ratio=args.min_edge_to_cost_ratio,
                            )
                            if not edge_ok:
                                stats.expired += 1
                                console.print(
                                    f"[yellow]SKIP COST[/yellow] {sig.symbol} residual={abs(snap.edge_bps):.2f}bps "
                                    f"roundtrip={rt_cost:.2f}bps required={required:.2f}bps fill={fill.fill_ratio:.1%}"
                                )
                            else:
                                stats.entries += 1
                                stats.fills.append(fill.fill_ratio)
                                stats.notionals.append(notional)
                                pos = Position(
                                    signal=sig,
                                    entry_ts_ms=now_ms,
                                    qty=fill.qty,
                                    entry_price=fill.avg_price,
                                    entry_mid=book.mid,
                                    entry_binance_price=snap.binance_mid,
                                    entry_residual_bps=snap.edge_bps,
                                    entry_spread_bps=book.spread_bps,
                                    entry_fill_ratio=fill.fill_ratio,
                                    entry_notional=notional,
                                    trailing=PositiveTrailing(distance_bps=max(args.trailing_distance_bps, book.spread_bps)),
                                )
                                console.print(
                                    f"[green]IOC PAPER ENTRY[/green] {sig.symbol} {'LONG' if sig.direction>0 else 'SHORT'} "
                                    f"requested=${args.target_notional_usdt:.0f} filled=${notional:.0f} ({fill.fill_ratio:.1%}) "
                                    f"price={fill.avg_price:g} residual={snap.edge_bps:+.2f}bps rt_cost={rt_cost:.2f}bps"
                                )

            if pos is not None:
                book = mexc.books.get(pos.signal.symbol)
                snap = models[pos.signal.symbol].snapshot(now_ms=now_ms, threshold_bps=0.0)
                if book is not None and _valid_snapshot(snap):
                    contract = by_symbol[pos.signal.symbol]
                    filled, exit_vwap = _exit_depth_for_qty(
                        book,
                        direction=pos.signal.direction,
                        qty=pos.qty,
                        contract_size=contract.contract_size,
                    )
                    if filled + 1e-12 >= pos.qty and exit_vwap > 0:
                        age_ms = now_ms - pos.entry_ts_ms
                        pnl_bps = _signed_move_bps(pos.signal.direction, pos.entry_price, exit_vwap)
                        pos.mfe_bps = max(pos.mfe_bps, pnl_bps)
                        pos.mae_bps = min(pos.mae_bps, pnl_bps)
                        trail = pos.trailing.update(pnl_bps)
                        mid_move = directional_move_bps(pos.signal.direction, pos.entry_mid, book.mid)
                        mexc_catchup = mid_move
                        leader_move = directional_move_bps(
                            pos.signal.direction, pos.entry_binance_price, snap.binance_mid
                        )
                        conv = max(args.convergence_bps, abs(pos.entry_residual_bps) * args.convergence_fraction)
                        residual_dir = 1 if snap.edge_bps > 0 else -1 if snap.edge_bps < 0 else 0
                        reason = None
                        if age_ms >= args.min_hold_ms:
                            if mid_move <= -args.mid_adverse_cut_bps:
                                reason = "mid_adverse_cut"
                            elif leader_move <= -args.leader_retrace_exit_bps:
                                reason = "leader_retrace"
                            elif residual_dir == -pos.signal.direction and abs(snap.edge_bps) >= args.reversal_edge_bps:
                                reason = "residual_reversal"
                            elif abs(snap.edge_bps) <= conv and mexc_catchup >= args.min_catchup_bps:
                                reason = "mexc_catchup_convergence"
                            elif trail is not None and pnl_bps <= trail:
                                reason = "positive_trailing_stop"
                            elif age_ms >= args.max_hold_ms:
                                reason = "timeout"
                        if reason is not None:
                            pnl_usdt = pos.qty * pos.entry_price * pnl_bps / 10_000.0
                            stats.pnl_usdt += pnl_usdt
                            stats.holds.append(float(age_ms))
                            stats.reasons[reason] = stats.reasons.get(reason, 0) + 1
                            if pnl_bps > 0:
                                stats.wins += 1
                                stats.gross_win_bps += pnl_bps
                            elif pnl_bps < 0:
                                stats.losses += 1
                                stats.gross_loss_bps += abs(pnl_bps)
                            else:
                                stats.flats += 1
                            console.print(
                                f"[{'green' if pnl_bps>0 else 'red'}]IOC PAPER EXIT[/] {pos.signal.symbol} {reason} "
                                f"pnl={pnl_bps:+.2f}bps ${pnl_usdt:+.2f} mid={mid_move:+.2f}bps "
                                f"catchup={mexc_catchup:+.2f}bps hold={age_ms}ms"
                            )
                            pos = None

            if now >= warmup_until and pending is None and pos is None:
                candidates = []
                for symbol in symbols:
                    if not fee_cache.fresh_zero(symbol, now_ms):
                        continue
                    book = mexc.books.get(symbol)
                    if book is None or now_ms - book.recv_ms > args.max_book_age_ms:
                        continue
                    model = models[symbol]
                    snap = model.snapshot(now_ms=now_ms, threshold_bps=0.0)
                    if not _valid_snapshot(snap):
                        continue
                    d = gate.observe(symbol, snap, book.spread_bps, now_ms, event_key=_event_key(model))
                    if not d.ready:
                        continue
                    strength = abs(d.residual_bps) / max(d.threshold_bps, 1e-12)
                    if strength < args.min_signal_strength_ratio or abs(d.residual_bps) < args.min_absolute_residual_bps:
                        continue
                    candidates.append((abs(d.residual_bps), strength, d.leader_advantage_bps, symbol, d, snap, book))
                if candidates:
                    _, _, _, symbol, d, snap, book = max(candidates, key=lambda row: (row[0], row[1], row[2]))
                    stats.signals += 1
                    sig = Signal(
                        signal_id=f"ioc-{stats.signals}-{now_ms}",
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
                    pending = Pending(sig, time.monotonic() + rtt / 1000.0)
                    console.print(
                        f"SIGNAL #{stats.signals} {symbol} {'LONG' if d.direction>0 else 'SHORT'} "
                        f"residual={d.residual_bps:+.2f}bps strength={abs(d.residual_bps)/max(d.threshold_bps,1e-12):.2f}x "
                        f"spread={book.spread_bps:.2f}bps"
                    )

            if now >= next_heartbeat:
                console.print("IOC PAPER heartbeat " + _summary(stats))
                next_heartbeat = now + args.heartbeat_seconds

            wake.clear()
            try:
                await asyncio.wait_for(wake.wait(), timeout=0.02)
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

    console.print("\n[bold]FINAL LIVE PARTIAL-IOC PAPER REPORT[/bold]")
    console.print(_summary(stats))


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="LIVE-data paper trader with true partial IOC LIMIT entry semantics")
    p.add_argument("--session-seconds", type=float, default=1800.0)
    p.add_argument("--max-signals", type=int, default=300)
    p.add_argument("--target-notional-usdt", type=float, default=10000.0)
    p.add_argument("--lifetime-csv", default="")
    p.add_argument("--pair-min-signals", type=int, default=4)
    p.add_argument("--pair-min-median-lifetime-ms", type=float, default=300.0)
    p.add_argument("--pair-min-survival-rate", type=float, default=0.50)
    p.add_argument("--pair-min-strength-ratio", type=float, default=1.50)
    p.add_argument("--micro-horizon-ms", type=int, default=100)
    p.add_argument("--baseline-seconds", type=float, default=8.0)
    p.add_argument("--baseline-exclusion-ms", type=int, default=1000)
    p.add_argument("--noise-window-ms", type=int, default=8000)
    p.add_argument("--residual-noise-multiplier", type=float, default=3.0)
    p.add_argument("--binance-noise-multiplier", type=float, default=1.5)
    p.add_argument("--min-edge-bps", type=float, default=2.0)
    p.add_argument("--min-net-edge-bps", type=float, default=0.5)
    p.add_argument("--edge-to-spread-ratio", type=float, default=1.2)
    p.add_argument("--min-binance-move-bps", type=float, default=1.0)
    p.add_argument("--min-leader-advantage-bps", type=float, default=1.0)
    p.add_argument("--min-lead-ratio", type=float, default=1.35)
    p.add_argument("--confirm-updates", type=int, default=2)
    p.add_argument("--confirm-ms", type=int, default=15)
    p.add_argument("--rearm-fraction", type=float, default=0.35)
    p.add_argument("--min-signal-strength-ratio", type=float, default=3.0)
    p.add_argument("--min-absolute-residual-bps", type=float, default=8.0)
    p.add_argument("--min-residual-retention", type=float, default=0.60)
    p.add_argument("--min-impulse-retention", type=float, default=0.75)
    p.add_argument("--min-edge-after-spread-bps", type=float, default=2.0)
    p.add_argument("--ioc-cross-bps", type=float, default=1.0)
    p.add_argument("--min-filled-notional-usdt", type=float, default=50.0)
    p.add_argument("--min-executable-net-edge-bps", type=float, default=2.0)
    p.add_argument("--min-edge-to-cost-ratio", type=float, default=1.50)
    p.add_argument("--max-binance-age-ms", type=float, default=300.0)
    p.add_argument("--max-mexc-age-ms", type=float, default=2000.0)
    p.add_argument("--max-book-age-ms", type=float, default=750.0)
    p.add_argument("--warmup-seconds", type=float, default=10.0)
    p.add_argument("--depth-limit", type=int, default=20)
    p.add_argument("--min-hold-ms", type=int, default=50)
    p.add_argument("--max-hold-ms", type=int, default=15000)
    p.add_argument("--convergence-bps", type=float, default=0.25)
    p.add_argument("--convergence-fraction", type=float, default=0.25)
    p.add_argument("--min-catchup-bps", type=float, default=1.0)
    p.add_argument("--leader-retrace-exit-bps", type=float, default=1.5)
    p.add_argument("--reversal-edge-bps", type=float, default=0.75)
    p.add_argument("--mid-adverse-cut-bps", type=float, default=3.0)
    p.add_argument("--trailing-distance-bps", type=float, default=1.5)
    p.add_argument("--rtt-samples", type=int, default=40)
    p.add_argument("--rtt-warmup-samples", type=int, default=3)
    p.add_argument("--rtt-interval-ms", type=float, default=100.0)
    p.add_argument("--heartbeat-seconds", type=float, default=5.0)
    return p


def main() -> None:
    asyncio.run(run(build_parser().parse_args()))


if __name__ == "__main__":
    main()
