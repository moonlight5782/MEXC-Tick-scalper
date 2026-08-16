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

from .lead_lag_strategy import LeadLagGate, convergence_threshold, spread_aware_adverse_cut
from .live_lead_lag_shadow import PositiveTrailing
from .live_production_runner import FeeCache, _fee_loop, _signed_move_bps
from .live_zero_fee_universe import LiveZeroFeeContract, discover_live_zero_fee_crosslisted
from .microspread import MicroSpreadModel
from .microspread_feed import EventBinanceBookTickerFeed, EventMexcDepthFeed, LiveBook
from .prelive_latency_diagnostic import _exit_depth_for_qty, _walk_depth

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


@dataclass(slots=True)
class Pending:
    signal: Signal
    execute_at: float


@dataclass(slots=True)
class SimPosition:
    signal: Signal
    entry_ts_ms: int
    qty: float
    entry_price: float
    entry_residual_bps: float
    entry_spread_bps: float
    trailing: PositiveTrailing
    mfe_bps: float = 0.0
    mae_bps: float = 0.0


@dataclass(slots=True)
class Stats:
    latency_ms: int
    pending: Pending | None = None
    position: SimPosition | None = None
    signals: int = 0
    entries: int = 0
    expired: int = 0
    busy: int = 0
    wins: int = 0
    losses: int = 0
    pnl_usdt: float = 0.0
    gross_win_bps: float = 0.0
    gross_loss_bps: float = 0.0
    edge_losses: list[float] = field(default_factory=list)
    holds: list[float] = field(default_factory=list)

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


def _append(path: Path, row: dict[str, object]) -> None:
    fields = [
        "event_ms", "signal_id", "latency_ms", "event", "symbol", "direction",
        "signal_residual_bps", "entry_residual_bps", "edge_lost_before_entry_bps",
        "threshold_bps", "noise_bps", "leader_advantage_bps", "signal_spread_bps",
        "entry_spread_bps", "entry_price", "exit_price", "qty", "fill_ratio",
        "hold_ms", "pnl_bps", "pnl_usdt", "mfe_bps", "mae_bps", "exit_reason",
    ]
    exists = path.exists()
    with path.open("a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        if not exists:
            w.writeheader()
        w.writerow({k: row.get(k, "") for k in fields})


def _summary(s: Stats) -> str:
    closed = s.wins + s.losses
    wr = s.wins / closed * 100 if closed else 0.0
    med_loss = statistics.median(s.edge_losses) if s.edge_losses else 0.0
    med_hold = statistics.median(s.holds) if s.holds else 0.0
    pf = "inf" if math.isinf(s.pf) else f"{s.pf:.3f}"
    return (
        f"{s.latency_ms:>3}ms signals={s.signals} entries={s.entries} expired={s.expired} busy={s.busy} "
        f"W/L={s.wins}/{s.losses} WR={wr:.1f}% PF={pf} pnl={s.pnl_usdt:+.4f}USDT "
        f"edge_loss_med={med_loss:.3f}bps hold_med={med_hold:.0f}ms"
    )


async def run(args: argparse.Namespace) -> None:
    contracts = await discover_live_zero_fee_crosslisted()
    included = {x.strip().upper() for x in args.include_symbols.split(",") if x.strip()}
    excluded = {x.strip().upper() for x in args.exclude_symbols.split(",") if x.strip()}
    if included:
        contracts = [x for x in contracts if x.mexc_symbol in included]
    if excluded:
        contracts = [x for x in contracts if x.mexc_symbol not in excluded]
    if not contracts:
        raise RuntimeError("No LIVE exact-0/0 Binance-crosslisted symbols remain")

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
    latencies = sorted({int(x) for x in args.latencies_ms.split(",") if x.strip() and int(x) >= 0})
    profiles = {x: Stats(latency_ms=x) for x in latencies}

    wake = asyncio.Event()
    binance = EventBinanceBookTickerFeed(contracts, models, wake)
    mexc = EventMexcDepthFeed(symbols, models, wake, depth_limit=args.depth_limit)
    await binance.start()
    await mexc.start()
    fee_cache = FeeCache()
    fee_stop = asyncio.Event()
    fee_task = asyncio.create_task(_fee_loop(fee_cache, fee_stop))

    output = Path(args.csv or f"prelive_latency_v2_{int(time.time())}.csv")
    deadline = time.monotonic() + args.session_seconds
    warmup_until = time.monotonic() + args.warmup_seconds
    next_heartbeat = 0.0
    signal_count = 0

    console.print(
        f"[cyan]PRE-LIVE V2 READ-ONLY[/cyan]: {len(symbols)} exact-0/0 symbols; "
        f"latencies={','.join(str(x) for x in latencies)}ms target={args.target_notional_usdt:g}USDT"
    )
    console.print("Adaptive noise + Binance-lead confirmation. No MEXC order writes.")
    console.print(f"CSV: {output.resolve()}")

    try:
        while time.monotonic() < deadline and signal_count < args.max_signals:
            now = time.monotonic()
            now_ms = int(time.time() * 1000)

            # Delayed virtual entries: opportunity must still be economically and directionally valid.
            for p in profiles.values():
                if p.pending is None or now < p.pending.execute_at:
                    continue
                pending = p.pending
                p.pending = None
                sig = pending.signal
                book = mexc.books.get(sig.symbol)
                snap = models[sig.symbol].snapshot(now_ms=now_ms, threshold_bps=0.0)
                if (
                    book is None or now_ms - book.recv_ms > args.max_book_age_ms
                    or not fee_cache.fresh_zero(sig.symbol, now_ms) or not _valid_snapshot(snap)
                ):
                    p.expired += 1
                    _append(output, {"event_ms": now_ms, "signal_id": sig.signal_id, "latency_ms": p.latency_ms,
                                     "event": "expired_before_entry", "symbol": sig.symbol, "direction": sig.direction})
                    continue
                d = gate.assess(sig.symbol, snap, book.spread_bps, now_ms)
                if not d.ready or d.direction != sig.direction:
                    p.expired += 1
                    _append(output, {
                        "event_ms": now_ms, "signal_id": sig.signal_id, "latency_ms": p.latency_ms,
                        "event": "expired_before_entry", "symbol": sig.symbol, "direction": sig.direction,
                        "signal_residual_bps": sig.residual_bps, "entry_residual_bps": d.residual_bps,
                        "edge_lost_before_entry_bps": abs(sig.residual_bps) - abs(d.residual_bps),
                        "threshold_bps": d.threshold_bps, "noise_bps": d.noise_bps,
                    })
                    continue
                contract = contract_by_symbol[sig.symbol]
                qty, vwap = _walk_depth(
                    book, direction=sig.direction, target_notional_usdt=args.target_notional_usdt,
                    contract_size=contract.contract_size, opening=True,
                )
                if qty <= 0 or vwap <= 0:
                    p.expired += 1
                    continue
                best = book.ask if sig.direction > 0 else book.bid
                requested = args.target_notional_usdt / best if best > 0 else 0.0
                fill_ratio = min(1.0, qty / requested) if requested > 0 else 0.0
                edge_loss = abs(sig.residual_bps) - abs(d.residual_bps)
                p.edge_losses.append(edge_loss)
                p.entries += 1
                p.position = SimPosition(
                    signal=sig, entry_ts_ms=now_ms, qty=qty, entry_price=vwap,
                    entry_residual_bps=d.residual_bps, entry_spread_bps=book.spread_bps,
                    trailing=PositiveTrailing(distance_bps=max(args.trailing_distance_bps, book.spread_bps)),
                )
                _append(output, {
                    "event_ms": now_ms, "signal_id": sig.signal_id, "latency_ms": p.latency_ms,
                    "event": "virtual_entry", "symbol": sig.symbol, "direction": sig.direction,
                    "signal_residual_bps": sig.residual_bps, "entry_residual_bps": d.residual_bps,
                    "edge_lost_before_entry_bps": edge_loss, "threshold_bps": d.threshold_bps,
                    "noise_bps": d.noise_bps, "leader_advantage_bps": d.leader_advantage_bps,
                    "signal_spread_bps": sig.spread_bps, "entry_spread_bps": book.spread_bps,
                    "entry_price": vwap, "qty": qty, "fill_ratio": fill_ratio,
                })

            # Same exit logic as LIVE V2: convergence is primary; adverse protection covers entry spread.
            for p in profiles.values():
                pos = p.position
                if pos is None:
                    continue
                book = mexc.books.get(pos.signal.symbol)
                if book is None:
                    continue
                contract = contract_by_symbol[pos.signal.symbol]
                filled, exit_vwap = _exit_depth_for_qty(
                    book, direction=pos.signal.direction, qty=pos.qty, contract_size=contract.contract_size,
                )
                if filled + 1e-12 < pos.qty or exit_vwap <= 0:
                    continue
                age_ms = now_ms - pos.entry_ts_ms
                move_bps = _signed_move_bps(pos.signal.direction, pos.entry_price, exit_vwap)
                pos.mfe_bps = max(pos.mfe_bps, move_bps)
                pos.mae_bps = min(pos.mae_bps, move_bps)
                trail = pos.trailing.update(move_bps)
                snap = models[pos.signal.symbol].snapshot(now_ms=now_ms, threshold_bps=0.0)
                conv = convergence_threshold(pos.entry_residual_bps, args.convergence_bps, args.convergence_fraction)
                adverse = spread_aware_adverse_cut(pos.entry_spread_bps, args.adverse_cut_bps, args.adverse_spread_multiple)
                residual_direction = 1 if snap.edge_bps > 0 else -1 if snap.edge_bps < 0 else 0
                reason = None
                if abs(snap.edge_bps) <= conv and age_ms >= args.min_hold_ms:
                    reason = "convergence"
                elif residual_direction == -pos.signal.direction and abs(snap.edge_bps) >= args.reversal_edge_bps and age_ms >= args.min_hold_ms:
                    reason = "residual_reversal"
                elif trail is not None and move_bps <= trail and age_ms >= args.min_hold_ms:
                    reason = "positive_trailing_stop"
                elif move_bps <= -adverse and age_ms >= args.min_hold_ms:
                    reason = "spread_aware_adverse_cut"
                elif age_ms >= args.max_hold_ms:
                    reason = "timeout"
                if reason is None:
                    continue
                pnl_usdt = pos.qty * pos.entry_price * move_bps / 10_000.0
                p.pnl_usdt += pnl_usdt
                p.holds.append(float(age_ms))
                if move_bps > 0:
                    p.wins += 1
                    p.gross_win_bps += move_bps
                elif move_bps < 0:
                    p.losses += 1
                    p.gross_loss_bps += abs(move_bps)
                _append(output, {
                    "event_ms": now_ms, "signal_id": pos.signal.signal_id, "latency_ms": p.latency_ms,
                    "event": "virtual_exit", "symbol": pos.signal.symbol, "direction": pos.signal.direction,
                    "signal_residual_bps": pos.signal.residual_bps, "entry_residual_bps": pos.entry_residual_bps,
                    "signal_spread_bps": pos.signal.spread_bps, "entry_spread_bps": pos.entry_spread_bps,
                    "entry_price": pos.entry_price, "exit_price": exit_vwap, "qty": pos.qty,
                    "hold_ms": age_ms, "pnl_bps": move_bps, "pnl_usdt": pnl_usdt,
                    "mfe_bps": pos.mfe_bps, "mae_bps": pos.mae_bps, "exit_reason": reason,
                })
                p.position = None

            if now >= warmup_until:
                emitted = []
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
                    if d.ready:
                        emitted.append((abs(d.residual_bps) - d.threshold_bps, -book.spread_bps, symbol, d, book))
                if emitted:
                    _, _, symbol, d, book = max(emitted, key=lambda x: (x[0], x[1]))
                    signal_count += 1
                    sig = Signal(
                        signal_id=uuid.uuid4().hex, ts_ms=now_ms, symbol=symbol, direction=d.direction,
                        residual_bps=d.residual_bps, threshold_bps=d.threshold_bps, noise_bps=d.noise_bps,
                        spread_bps=book.spread_bps, leader_advantage_bps=d.leader_advantage_bps,
                    )
                    for p in profiles.values():
                        p.signals += 1
                        if p.position is not None or p.pending is not None:
                            p.busy += 1
                            continue
                        p.pending = Pending(sig, time.monotonic() + p.latency_ms / 1000.0)
                    console.print(
                        f"SIGNAL #{signal_count} {symbol} {'LONG' if d.direction > 0 else 'SHORT'} "
                        f"residual={d.residual_bps:+.3f} threshold={d.threshold_bps:.3f} noise={d.noise_bps:.3f} "
                        f"Bmove={d.binance_move_bps:+.3f} Mmove={d.mexc_move_bps:+.3f} lead={d.leader_advantage_bps:.3f}bps"
                    )

            if now >= next_heartbeat:
                console.print(
                    f"READ-ONLY V2 heartbeat signals={signal_count}/{args.max_signals} Bquotes={binance.quotes} "
                    f"Mdepth={mexc.updates} books={len(mexc.books)}/{len(symbols)}"
                )
                for p in profiles.values():
                    console.print("  " + _summary(p))
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

    console.print("\n[bold]FINAL V2 SAME-SIGNAL LATENCY COMPARISON[/bold]")
    for p in profiles.values():
        console.print(_summary(p))
    console.print(f"Detailed per-signal comparison: {output.resolve()}")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Read-only noise-filtered Binance-lead/MEXC-lag latency diagnostic")
    p.add_argument("--session-seconds", type=float, default=1800.0)
    p.add_argument("--max-signals", type=int, default=300)
    p.add_argument("--latencies-ms", default="0,25,50,75,100,150,250")
    p.add_argument("--target-notional-usdt", type=float, default=10000.0)
    p.add_argument("--include-symbols", default="")
    p.add_argument("--exclude-symbols", default="")
    p.add_argument("--micro-horizon-ms", type=int, default=100)
    p.add_argument("--baseline-seconds", type=float, default=8.0)
    p.add_argument("--baseline-exclusion-ms", type=int, default=1000)
    p.add_argument("--noise-window-ms", type=int, default=8000)
    p.add_argument("--residual-noise-multiplier", type=float, default=3.0)
    p.add_argument("--binance-noise-multiplier", type=float, default=1.0)
    p.add_argument("--min-edge-bps", type=float, default=0.50)
    p.add_argument("--min-net-edge-bps", type=float, default=0.20)
    p.add_argument("--edge-to-spread-ratio", type=float, default=1.05)
    p.add_argument("--min-binance-move-bps", type=float, default=0.50)
    p.add_argument("--min-leader-advantage-bps", type=float, default=0.25)
    p.add_argument("--min-lead-ratio", type=float, default=1.25)
    p.add_argument("--confirm-updates", type=int, default=2)
    p.add_argument("--confirm-ms", type=int, default=15)
    p.add_argument("--rearm-fraction", type=float, default=0.35)
    p.add_argument("--max-binance-age-ms", type=float, default=300.0)
    p.add_argument("--max-mexc-age-ms", type=float, default=2000.0)
    p.add_argument("--max-book-age-ms", type=float, default=750.0)
    p.add_argument("--warmup-seconds", type=float, default=10.0)
    p.add_argument("--depth-limit", type=int, default=20)
    p.add_argument("--min-hold-ms", type=int, default=50)
    p.add_argument("--max-hold-ms", type=int, default=15000)
    p.add_argument("--convergence-bps", type=float, default=0.10)
    p.add_argument("--convergence-fraction", type=float, default=0.20)
    p.add_argument("--reversal-edge-bps", type=float, default=0.35)
    p.add_argument("--adverse-cut-bps", type=float, default=1.5)
    p.add_argument("--adverse-spread-multiple", type=float, default=1.25)
    p.add_argument("--trailing-distance-bps", type=float, default=1.5)
    p.add_argument("--heartbeat-seconds", type=float, default=5.0)
    p.add_argument("--csv", default="")
    return p


def main() -> None:
    args = build_parser().parse_args()
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
