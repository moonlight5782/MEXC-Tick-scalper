from __future__ import annotations

import argparse
import asyncio
import csv
import math
import statistics
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from rich.console import Console

from .lead_lag_strategy import LeadLagGate, spread_aware_adverse_cut
from .live_lead_lag_shadow import PositiveTrailing
from .live_production_runner import FeeCache, _fee_loop, _signed_move_bps
from .live_zero_fee_universe import LiveZeroFeeContract, discover_live_zero_fee_crosslisted
from .microspread import MicroSpreadModel
from .microspread_feed import EventBinanceBookTickerFeed, EventMexcDepthFeed
from .persistent_lag_profile import build_profiles, latest_lifetime_csv, select_profiles
from .prelive_latency_diagnostic import _exit_depth_for_qty, _walk_depth
from .prelive_measured_rtt_diagnostic import measure_live_private_rtt, _percentile

console = Console()


@dataclass(frozen=True, slots=True)
class Signal:
    signal_id: str
    ts_ms: int
    symbol: str
    direction: int
    residual_bps: float
    threshold_bps: float
    noise_bps: float
    spread_bps: float
    leader_advantage_bps: float
    binance_move_bps: float
    mexc_move_bps: float
    binance_price: float
    mexc_price: float


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
    trailing: PositiveTrailing
    mfe_bps: float = 0.0
    mae_bps: float = 0.0


@dataclass(slots=True)
class Stats:
    signals: int = 0
    entries: int = 0
    expired: int = 0
    busy: int = 0
    wins: int = 0
    losses: int = 0
    flats: int = 0
    pnl_usdt: float = 0.0
    gross_win_bps: float = 0.0
    gross_loss_bps: float = 0.0
    holds: list[float] = field(default_factory=list)
    entry_retention: list[float] = field(default_factory=list)
    exit_reasons: dict[str, int] = field(default_factory=dict)

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
        "warming_up", "warming_baseline", "warming_horizon",
        "stale_binance", "stale_mexc",
    }


def directional_move_bps(direction: int, start: float, end: float) -> float:
    if start <= 0 or end <= 0 or direction not in (-1, 1):
        return 0.0
    return direction * (end / start - 1.0) * 10_000.0


def impulse_retention_fraction(direction: int, signal_binance_price: float, signal_binance_move_bps: float,
                               current_binance_price: float) -> float:
    move = direction * signal_binance_move_bps
    if move <= 0 or signal_binance_price <= 0 or current_binance_price <= 0:
        return 0.0
    pre_price = signal_binance_price / (1.0 + direction * move / 10_000.0)
    original = directional_move_bps(direction, pre_price, signal_binance_price)
    current = directional_move_bps(direction, pre_price, current_binance_price)
    if original <= 0:
        return 0.0
    return current / original


def delayed_catchup_entry_ok(*, signal: Signal, current_residual_bps: float, current_binance_price: float,
                             current_spread_bps: float, min_residual_retention: float,
                             min_impulse_retention: float, min_remaining_edge_bps: float,
                             min_edge_after_spread_bps: float) -> tuple[bool, str, float, float]:
    if signal.direction * current_residual_bps <= 0:
        return False, "residual_reversed", 0.0, 0.0
    residual_retention = abs(current_residual_bps) / max(abs(signal.residual_bps), 1e-12)
    if residual_retention < min_residual_retention:
        return False, "residual_retention_low", residual_retention, 0.0
    impulse_retention = impulse_retention_fraction(
        signal.direction, signal.binance_price, signal.binance_move_bps, current_binance_price
    )
    if impulse_retention < min_impulse_retention:
        return False, "leader_retraced_before_entry", residual_retention, impulse_retention
    required = max(min_remaining_edge_bps, current_spread_bps + min_edge_after_spread_bps)
    if abs(current_residual_bps) < required:
        return False, "remaining_edge_too_small", residual_retention, impulse_retention
    return True, "exploitable", residual_retention, impulse_retention


def _append(path: Path, row: dict[str, object]) -> None:
    fields = [
        "event_ms", "signal_id", "event", "symbol", "direction", "signal_residual_bps",
        "signal_threshold_bps", "signal_strength_ratio", "signal_binance_move_bps",
        "signal_mexc_move_bps", "signal_lead_bps", "signal_spread_bps", "entry_residual_bps",
        "residual_retention", "impulse_retention", "entry_spread_bps", "entry_price", "exit_price",
        "qty", "hold_ms", "pnl_bps", "pnl_usdt", "mfe_bps", "mae_bps", "mexc_catchup_bps",
        "leader_move_from_entry_bps", "exit_reason",
    ]
    exists = path.exists()
    with path.open("a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        if not exists:
            w.writeheader()
        w.writerow({k: row.get(k, "") for k in fields})


def _summary(stats: Stats) -> str:
    closed = stats.wins + stats.losses + stats.flats
    wr = stats.wins / closed * 100 if closed else 0.0
    pf = "inf" if math.isinf(stats.pf) else f"{stats.pf:.3f}"
    hold = statistics.median(stats.holds) if stats.holds else 0.0
    retention = statistics.median(stats.entry_retention) if stats.entry_retention else 0.0
    reasons = ",".join(f"{k}:{v}" for k, v in sorted(stats.exit_reasons.items())) or "-"
    return (
        f"signals={stats.signals} entries={stats.entries} expired={stats.expired} busy={stats.busy} "
        f"W/L/F={stats.wins}/{stats.losses}/{stats.flats} WR={wr:.1f}% PF={pf} "
        f"pnl={stats.pnl_usdt:+.4f}USDT hold_med={hold:.0f}ms retention_med={retention:.2f} exits={reasons}"
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

    console.print("[bold cyan]LIVE PAPER CATCH-UP MODE[/bold cyan] - NO REAL ORDERS")
    console.print(f"Lifetime source: {source.resolve()}")
    for p in profiles:
        if p.symbol in {x.mexc_symbol for x in contracts}:
            console.print(
                f"  KEEP {p.symbol}: n={p.signals} median={p.median_lifetime_ms:.0f}ms "
                f"survive={p.survive_execution_rate*100:.1f}% strength={p.median_signal_strength_ratio:.2f}x"
            )

    rtts = await measure_live_private_rtt(
        samples=args.rtt_samples, warmup_samples=args.rtt_warmup_samples, interval_ms=args.rtt_interval_ms
    )
    rtt = statistics.median(rtts)
    console.print(
        f"Measured LIVE private RTT: median={rtt:.1f}ms p95={_percentile(rtts, .95):.1f}ms "
        f"min={min(rtts):.1f}ms max={max(rtts):.1f}ms"
    )

    symbols = [x.mexc_symbol for x in contracts]
    contract_by_symbol: dict[str, LiveZeroFeeContract] = {x.mexc_symbol: x for x in contracts}
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

    pending: Pending | None = None
    position: Position | None = None
    stats = Stats()
    output = Path(args.csv or f"prelive_persistent_catchup_{int(time.time())}.csv")
    deadline = time.monotonic() + args.session_seconds
    warmup_until = time.monotonic() + args.warmup_seconds
    next_heartbeat = 0.0

    console.print(
        f"Entry gate: strength>={args.min_signal_strength_ratio:.2f}x residual-retention>={args.min_residual_retention:.0%} "
        f"leader-retention>={args.min_impulse_retention:.0%} remaining-edge>=spread+{args.min_edge_after_spread_bps:.2f}bps"
    )
    console.print(f"CSV: {output.resolve()}")

    try:
        while time.monotonic() < deadline and stats.signals < args.max_signals:
            now = time.monotonic()
            now_ms = int(time.time() * 1000)

            if pending is not None and position is None and now >= pending.execute_at:
                sig = pending.signal
                pending = None
                book = mexc.books.get(sig.symbol)
                snap = models[sig.symbol].snapshot(now_ms=now_ms, threshold_bps=0.0)
                if (
                    book is None or now_ms - book.recv_ms > args.max_book_age_ms
                    or not fee_cache.fresh_zero(sig.symbol, now_ms) or not _valid_snapshot(snap)
                ):
                    stats.expired += 1
                else:
                    ok, why, residual_ret, impulse_ret = delayed_catchup_entry_ok(
                        signal=sig,
                        current_residual_bps=snap.edge_bps,
                        current_binance_price=snap.binance_price,
                        current_spread_bps=book.spread_bps,
                        min_residual_retention=args.min_residual_retention,
                        min_impulse_retention=args.min_impulse_retention,
                        min_remaining_edge_bps=args.min_remaining_edge_bps,
                        min_edge_after_spread_bps=args.min_edge_after_spread_bps,
                    )
                    if not ok:
                        stats.expired += 1
                        _append(output, {
                            "event_ms": now_ms, "signal_id": sig.signal_id, "event": "expired_before_entry",
                            "symbol": sig.symbol, "direction": sig.direction, "signal_residual_bps": sig.residual_bps,
                            "entry_residual_bps": snap.edge_bps, "residual_retention": residual_ret,
                            "impulse_retention": impulse_ret, "exit_reason": why,
                        })
                    else:
                        contract = contract_by_symbol[sig.symbol]
                        qty, vwap = _walk_depth(
                            book, direction=sig.direction, target_notional_usdt=args.target_notional_usdt,
                            contract_size=contract.contract_size, opening=True,
                        )
                        if qty <= 0 or vwap <= 0:
                            stats.expired += 1
                        else:
                            stats.entries += 1
                            stats.entry_retention.append(residual_ret)
                            position = Position(
                                signal=sig,
                                entry_ts_ms=now_ms,
                                qty=qty,
                                entry_price=vwap,
                                entry_mid=book.mid,
                                entry_binance_price=snap.binance_price,
                                entry_residual_bps=snap.edge_bps,
                                entry_spread_bps=book.spread_bps,
                                trailing=PositiveTrailing(distance_bps=max(args.trailing_distance_bps, book.spread_bps)),
                            )
                            _append(output, {
                                "event_ms": now_ms, "signal_id": sig.signal_id, "event": "virtual_entry",
                                "symbol": sig.symbol, "direction": sig.direction,
                                "signal_residual_bps": sig.residual_bps, "signal_threshold_bps": sig.threshold_bps,
                                "signal_strength_ratio": abs(sig.residual_bps) / max(sig.threshold_bps, 1e-12),
                                "signal_binance_move_bps": sig.binance_move_bps,
                                "signal_mexc_move_bps": sig.mexc_move_bps, "signal_lead_bps": sig.leader_advantage_bps,
                                "signal_spread_bps": sig.spread_bps, "entry_residual_bps": snap.edge_bps,
                                "residual_retention": residual_ret, "impulse_retention": impulse_ret,
                                "entry_spread_bps": book.spread_bps, "entry_price": vwap, "qty": qty,
                            })
                            console.print(
                                f"[green]PAPER ENTRY[/green] {sig.symbol} {'LONG' if sig.direction > 0 else 'SHORT'} "
                                f"residual={snap.edge_bps:+.2f}bps ret={residual_ret:.0%} leader={impulse_ret:.0%} "
                                f"spread={book.spread_bps:.2f}bps"
                            )

            if position is not None:
                pos = position
                book = mexc.books.get(pos.signal.symbol)
                snap = models[pos.signal.symbol].snapshot(now_ms=now_ms, threshold_bps=0.0)
                if book is not None and _valid_snapshot(snap):
                    contract = contract_by_symbol[pos.signal.symbol]
                    filled, exit_vwap = _exit_depth_for_qty(
                        book, direction=pos.signal.direction, qty=pos.qty, contract_size=contract.contract_size
                    )
                    if filled + 1e-12 >= pos.qty and exit_vwap > 0:
                        age_ms = now_ms - pos.entry_ts_ms
                        pnl_bps = _signed_move_bps(pos.signal.direction, pos.entry_price, exit_vwap)
                        pos.mfe_bps = max(pos.mfe_bps, pnl_bps)
                        pos.mae_bps = min(pos.mae_bps, pnl_bps)
                        trail = pos.trailing.update(pnl_bps)
                        mexc_catchup = directional_move_bps(pos.signal.direction, pos.entry_mid, book.mid)
                        leader_move = directional_move_bps(pos.signal.direction, pos.entry_binance_price, snap.binance_price)
                        conv_threshold = max(args.convergence_bps, abs(pos.entry_residual_bps) * args.convergence_fraction)
                        residual_dir = 1 if snap.edge_bps > 0 else -1 if snap.edge_bps < 0 else 0
                        required_catchup = max(
                            args.min_catchup_bps,
                            abs(pos.entry_residual_bps) * args.min_catchup_fraction,
                            pos.entry_spread_bps + args.min_realized_edge_bps,
                        )
                        adverse = spread_aware_adverse_cut(
                            pos.entry_spread_bps, args.adverse_cut_bps, args.adverse_spread_multiple
                        )
                        reason = None
                        if age_ms >= args.min_hold_ms:
                            if leader_move <= -args.leader_retrace_exit_bps:
                                reason = "leader_retrace"
                            elif residual_dir == -pos.signal.direction and abs(snap.edge_bps) >= args.reversal_edge_bps:
                                reason = "residual_reversal"
                            elif abs(snap.edge_bps) <= conv_threshold and mexc_catchup >= required_catchup:
                                reason = "mexc_catchup_convergence"
                            elif trail is not None and pnl_bps <= trail:
                                reason = "positive_trailing_stop"
                            elif pnl_bps <= -adverse:
                                reason = "spread_aware_adverse_cut"
                            elif age_ms >= args.max_hold_ms:
                                reason = "timeout"
                        if reason is not None:
                            pnl_usdt = pos.qty * pos.entry_price * pnl_bps / 10_000.0
                            stats.pnl_usdt += pnl_usdt
                            stats.holds.append(float(age_ms))
                            stats.exit_reasons[reason] = stats.exit_reasons.get(reason, 0) + 1
                            if pnl_bps > 0:
                                stats.wins += 1
                                stats.gross_win_bps += pnl_bps
                            elif pnl_bps < 0:
                                stats.losses += 1
                                stats.gross_loss_bps += abs(pnl_bps)
                            else:
                                stats.flats += 1
                            _append(output, {
                                "event_ms": now_ms, "signal_id": pos.signal.signal_id, "event": "virtual_exit",
                                "symbol": pos.signal.symbol, "direction": pos.signal.direction,
                                "signal_residual_bps": pos.signal.residual_bps,
                                "entry_residual_bps": pos.entry_residual_bps, "entry_spread_bps": pos.entry_spread_bps,
                                "entry_price": pos.entry_price, "exit_price": exit_vwap, "qty": pos.qty,
                                "hold_ms": age_ms, "pnl_bps": pnl_bps, "pnl_usdt": pnl_usdt,
                                "mfe_bps": pos.mfe_bps, "mae_bps": pos.mae_bps,
                                "mexc_catchup_bps": mexc_catchup, "leader_move_from_entry_bps": leader_move,
                                "exit_reason": reason,
                            })
                            console.print(
                                f"[{'green' if pnl_bps > 0 else 'red'}]PAPER EXIT[/] {pos.signal.symbol} {reason} "
                                f"pnl={pnl_bps:+.2f}bps ${pnl_usdt:+.2f} catchup={mexc_catchup:+.2f}bps "
                                f"leader={leader_move:+.2f}bps hold={age_ms}ms"
                            )
                            position = None

            if now >= warmup_until and pending is None and position is None:
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
                    if strength < args.min_signal_strength_ratio:
                        continue
                    remaining_after_spread = abs(d.residual_bps) - book.spread_bps
                    if remaining_after_spread < args.min_edge_after_spread_bps:
                        continue
                    candidates.append((strength, abs(d.residual_bps), d.leader_advantage_bps, symbol, d, snap, book))
                if candidates:
                    _, _, _, symbol, d, snap, book = max(candidates, key=lambda x: (x[0], x[1], x[2]))
                    stats.signals += 1
                    sig = Signal(
                        signal_id=uuid.uuid4().hex,
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
                        binance_price=snap.binance_price,
                        mexc_price=snap.mexc_price,
                    )
                    pending = Pending(sig, time.monotonic() + rtt / 1000.0)
                    console.print(
                        f"SIGNAL #{stats.signals} {symbol} {'LONG' if d.direction > 0 else 'SHORT'} "
                        f"residual={d.residual_bps:+.2f} thr={d.threshold_bps:.2f} "
                        f"strength={abs(d.residual_bps)/max(d.threshold_bps,1e-12):.2f}x "
                        f"Bmove={d.binance_move_bps:+.2f} Mmove={d.mexc_move_bps:+.2f} spread={book.spread_bps:.2f}"
                    )

            if now >= next_heartbeat:
                console.print("PAPER heartbeat " + _summary(stats))
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

    console.print("\n[bold]FINAL LIVE PAPER CATCH-UP REPORT[/bold]")
    console.print(_summary(stats))
    console.print(f"Detailed CSV: {output.resolve()}")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="LIVE-data persistent catch-up paper trader; never submits MEXC orders")
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
    p.add_argument("--min-net-edge-bps", type=float, default=0.50)
    p.add_argument("--edge-to-spread-ratio", type=float, default=1.20)
    p.add_argument("--min-binance-move-bps", type=float, default=1.0)
    p.add_argument("--min-leader-advantage-bps", type=float, default=1.0)
    p.add_argument("--min-lead-ratio", type=float, default=1.35)
    p.add_argument("--confirm-updates", type=int, default=2)
    p.add_argument("--confirm-ms", type=int, default=15)
    p.add_argument("--rearm-fraction", type=float, default=0.35)
    p.add_argument("--min-signal-strength-ratio", type=float, default=2.0)
    p.add_argument("--min-residual-retention", type=float, default=0.55)
    p.add_argument("--min-impulse-retention", type=float, default=0.70)
    p.add_argument("--min-remaining-edge-bps", type=float, default=2.5)
    p.add_argument("--min-edge-after-spread-bps", type=float, default=1.5)
    p.add_argument("--max-binance-age-ms", type=float, default=300.0)
    p.add_argument("--max-mexc-age-ms", type=float, default=2000.0)
    p.add_argument("--max-book-age-ms", type=float, default=750.0)
    p.add_argument("--warmup-seconds", type=float, default=10.0)
    p.add_argument("--depth-limit", type=int, default=20)
    p.add_argument("--min-hold-ms", type=int, default=50)
    p.add_argument("--max-hold-ms", type=int, default=5000)
    p.add_argument("--convergence-bps", type=float, default=0.25)
    p.add_argument("--convergence-fraction", type=float, default=0.25)
    p.add_argument("--min-catchup-bps", type=float, default=1.0)
    p.add_argument("--min-catchup-fraction", type=float, default=0.45)
    p.add_argument("--min-realized-edge-bps", type=float, default=0.25)
    p.add_argument("--leader-retrace-exit-bps", type=float, default=1.5)
    p.add_argument("--reversal-edge-bps", type=float, default=0.75)
    p.add_argument("--adverse-cut-bps", type=float, default=2.0)
    p.add_argument("--adverse-spread-multiple", type=float, default=1.50)
    p.add_argument("--trailing-distance-bps", type=float, default=1.5)
    p.add_argument("--rtt-samples", type=int, default=40)
    p.add_argument("--rtt-warmup-samples", type=int, default=3)
    p.add_argument("--rtt-interval-ms", type=float, default=100.0)
    p.add_argument("--heartbeat-seconds", type=float, default=5.0)
    p.add_argument("--csv", default="")
    return p


def main() -> None:
    asyncio.run(run(build_parser().parse_args()))


if __name__ == "__main__":
    main()
