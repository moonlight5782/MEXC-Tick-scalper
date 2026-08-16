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

from .execution import OrderSide
from .live_lead_lag_shadow import PositiveTrailing
from .live_zero_fee_universe import LiveZeroFeeContract, discover_live_zero_fee_crosslisted
from .microspread import MicroSpreadModel
from .microspread_feed import EventBinanceBookTickerFeed, EventMexcDepthFeed, LiveBook
from .web_execution import MexcWebExecutionAdapter, WebExecutionConfig
from .web_fee import read_web_fee_provider

console = Console()
FEE_REFRESH_SECONDS = 2.0
FEE_MAX_AGE_MS = 3500


@dataclass(slots=True)
class FeeCache:
    provider: object | None = None
    checked_ms: int = 0
    error: str = "not_started"

    def fresh_zero(self, symbol: str, now_ms: int) -> bool:
        if self.provider is None or now_ms - self.checked_ms > FEE_MAX_AGE_MS:
            return False
        status = self.provider.status(symbol)
        return status.maker == 0 and status.taker == 0


@dataclass(frozen=True, slots=True)
class Signal:
    signal_id: str
    ts_ms: int
    symbol: str
    direction: int
    edge_bps: float
    threshold_bps: float
    spread_bps: float
    compute_us: float


@dataclass(slots=True)
class PendingEntry:
    signal: Signal
    execute_at: float


@dataclass(slots=True)
class SimPosition:
    signal: Signal
    entry_ts_ms: int
    qty: float
    entry_price: float
    entry_edge_bps: float
    entry_spread_bps: float
    trailing: PositiveTrailing
    mfe_bps: float = 0.0
    mae_bps: float = 0.0


@dataclass(slots=True)
class ProfileStats:
    latency_ms: int
    pending: PendingEntry | None = None
    position: SimPosition | None = None
    signals_seen: int = 0
    entries: int = 0
    expired: int = 0
    busy: int = 0
    no_depth: int = 0
    wins: int = 0
    losses: int = 0
    pnl_bps: float = 0.0
    pnl_usdt: float = 0.0
    gross_win_bps: float = 0.0
    gross_loss_bps: float = 0.0
    holds_ms: list[float] = field(default_factory=list)
    edge_losses_bps: list[float] = field(default_factory=list)

    @property
    def pf(self) -> float:
        if self.gross_loss_bps <= 0:
            return math.inf if self.gross_win_bps > 0 else 0.0
        return self.gross_win_bps / self.gross_loss_bps


def _required_edge(spread_bps: float, args: argparse.Namespace) -> float:
    return max(
        float(args.min_edge_bps),
        float(spread_bps) + float(args.min_net_edge_bps),
        float(spread_bps) * float(args.edge_to_spread_ratio),
    )


def _signed_move_bps(direction: int, entry: float, current: float) -> float:
    if direction not in (-1, 1) or entry <= 0 or current <= 0:
        return 0.0
    return direction * (current - entry) / entry * 10000.0


def _walk_depth(
    book: LiveBook,
    *,
    direction: int,
    target_notional_usdt: float,
    contract_size: float,
    opening: bool,
) -> tuple[float, float]:
    """Return (filled_base_qty, vwap) using the currently visible LIVE depth.

    MEXC depth volume is treated as contract volume; contract_size converts it
    to base-asset quantity. An IOC accepts partial depth and never tops up.
    """
    if target_notional_usdt <= 0 or contract_size <= 0:
        return 0.0, 0.0
    buy = (direction > 0 and opening) or (direction < 0 and not opening)
    levels = book.asks if buy else book.bids
    if not levels:
        return 0.0, 0.0
    remaining = float(target_notional_usdt)
    filled_qty = 0.0
    quote = 0.0
    for price, contracts in levels:
        if price <= 0 or contracts <= 0:
            continue
        base_available = float(contracts) * float(contract_size)
        quote_available = base_available * float(price)
        take_quote = min(remaining, quote_available)
        take_qty = take_quote / float(price)
        filled_qty += take_qty
        quote += take_quote
        remaining -= take_quote
        if remaining <= 1e-9:
            break
    return filled_qty, (quote / filled_qty if filled_qty > 0 else 0.0)


def _exit_depth_for_qty(
    book: LiveBook,
    *,
    direction: int,
    qty: float,
    contract_size: float,
) -> tuple[float, float]:
    if qty <= 0 or contract_size <= 0:
        return 0.0, 0.0
    buy = direction < 0
    levels = book.asks if buy else book.bids
    remaining_qty = qty
    filled = 0.0
    quote = 0.0
    for price, contracts in levels:
        if price <= 0 or contracts <= 0:
            continue
        base_available = float(contracts) * float(contract_size)
        take = min(remaining_qty, base_available)
        filled += take
        quote += take * float(price)
        remaining_qty -= take
        if remaining_qty <= 1e-12:
            break
    return filled, (quote / filled if filled > 0 else 0.0)


def _append_csv(path: Path, row: dict[str, object]) -> None:
    fields = [
        "event_ms", "signal_id", "latency_ms", "event", "symbol", "direction",
        "signal_edge_bps", "entry_edge_bps", "edge_lost_before_entry_bps",
        "signal_spread_bps", "entry_spread_bps", "threshold_bps", "compute_us",
        "entry_price", "exit_price", "qty", "fill_ratio", "hold_ms", "pnl_bps",
        "pnl_usdt", "mfe_bps", "mae_bps", "exit_reason",
    ]
    exists = path.exists()
    with path.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        if not exists:
            writer.writeheader()
        writer.writerow({name: row.get(name, "") for name in fields})


async def _fee_loop(cache: FeeCache, stop: asyncio.Event) -> None:
    cfg = WebExecutionConfig.from_env(write_enabled=False)
    async with MexcWebExecutionAdapter(cfg) as adapter:
        while not stop.is_set():
            try:
                cache.provider = await read_web_fee_provider(adapter)
                cache.checked_ms = int(time.time() * 1000)
                cache.error = ""
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                cache.checked_ms = 0
                cache.error = f"{type(exc).__name__}: {exc}"
            try:
                await asyncio.wait_for(stop.wait(), timeout=FEE_REFRESH_SECONDS)
            except TimeoutError:
                pass


def _candidate_signal(
    symbol: str,
    model: MicroSpreadModel,
    book: LiveBook,
    args: argparse.Namespace,
    now_ms: int,
) -> Signal | None:
    threshold = _required_edge(book.spread_bps, args)
    started = time.perf_counter_ns()
    snap = model.signal(now_ms=now_ms, threshold_bps=threshold)
    compute_us = (time.perf_counter_ns() - started) / 1000.0
    if not snap.ready:
        return None
    return Signal(
        signal_id=uuid.uuid4().hex,
        ts_ms=now_ms,
        symbol=symbol,
        direction=int(snap.direction),
        edge_bps=float(snap.edge_bps),
        threshold_bps=float(threshold),
        spread_bps=float(book.spread_bps),
        compute_us=float(compute_us),
    )


def _summary_line(profile: ProfileStats) -> str:
    closed = profile.wins + profile.losses
    wr = profile.wins / closed * 100 if closed else 0.0
    median_edge_loss = statistics.median(profile.edge_losses_bps) if profile.edge_losses_bps else 0.0
    median_hold = statistics.median(profile.holds_ms) if profile.holds_ms else 0.0
    pf = "inf" if math.isinf(profile.pf) else f"{profile.pf:.3f}"
    return (
        f"{profile.latency_ms:>3}ms signals={profile.signals_seen} entries={profile.entries} "
        f"expired={profile.expired} busy={profile.busy} W/L={profile.wins}/{profile.losses} "
        f"WR={wr:.1f}% PF={pf} pnl={profile.pnl_usdt:+.4f}USDT "
        f"edge_loss_med={median_edge_loss:.3f}bps hold_med={median_hold:.0f}ms"
    )


async def run(args: argparse.Namespace) -> None:
    # This module is structurally read-only: no adapter is ever constructed with
    # write_enabled=True and no POST/order method is called.
    contracts = await discover_live_zero_fee_crosslisted()
    included = {x.strip().upper() for x in args.include_symbols.split(",") if x.strip()}
    excluded = {x.strip().upper() for x in args.exclude_symbols.split(",") if x.strip()}
    if included:
        contracts = [row for row in contracts if row.mexc_symbol in included]
    if excluded:
        contracts = [row for row in contracts if row.mexc_symbol not in excluded]
    if not contracts:
        raise RuntimeError("No LIVE exact-0/0 Binance-crosslisted symbols remain after filters")

    contract_by_symbol: dict[str, LiveZeroFeeContract] = {row.mexc_symbol: row for row in contracts}
    symbols = list(contract_by_symbol)
    models = {
        symbol: MicroSpreadModel(
            horizon_ms=args.micro_horizon_ms,
            baseline_seconds=args.baseline_seconds,
            baseline_exclusion_ms=args.baseline_exclusion_ms,
            min_edge_bps=args.min_edge_bps,
            min_binance_move_bps=args.min_binance_move_bps,
            max_binance_age_ms=args.max_binance_age_ms,
            max_mexc_age_ms=args.max_mexc_age_ms,
            rearm_fraction=args.rearm_fraction,
        )
        for symbol in symbols
    }
    latencies = sorted({int(x) for x in args.latencies_ms.split(",") if x.strip() and int(x) >= 0})
    if not latencies:
        raise ValueError("At least one non-negative latency is required")
    profiles = {latency: ProfileStats(latency_ms=latency) for latency in latencies}

    wake = asyncio.Event()
    binance = EventBinanceBookTickerFeed(contracts, models, wake)
    mexc = EventMexcDepthFeed(symbols, models, wake, depth_limit=args.depth_limit)
    await binance.start()
    await mexc.start()

    fee_cache = FeeCache()
    fee_stop = asyncio.Event()
    fee_task = asyncio.create_task(_fee_loop(fee_cache, fee_stop))
    output = Path(args.csv or f"prelive_latency_{int(time.time())}.csv")
    deadline = time.monotonic() + args.session_seconds
    warmup_until = time.monotonic() + args.warmup_seconds
    next_heartbeat = 0.0
    signal_count = 0

    console.print(
        f"[cyan]PRE-LIVE READ-ONLY LATENCY DIAGNOSTIC[/cyan]: {len(symbols)} LIVE exact-0/0 symbols; "
        f"latencies={','.join(str(x) for x in latencies)}ms; target={args.target_notional_usdt:g}USDT"
    )
    console.print("No MEXC order/position write requests are made by this runner.")
    console.print(f"CSV: {output.resolve()}")

    try:
        while time.monotonic() < deadline and signal_count < args.max_signals:
            now = time.monotonic()
            now_ms = int(time.time() * 1000)

            # Execute delayed virtual entries against the then-current real book.
            for profile in profiles.values():
                pending = profile.pending
                if pending is not None and now >= pending.execute_at:
                    profile.pending = None
                    sig = pending.signal
                    book = mexc.books.get(sig.symbol)
                    model = models[sig.symbol]
                    if book is None or now_ms - book.recv_ms > args.max_book_age_ms or not fee_cache.fresh_zero(sig.symbol, now_ms):
                        profile.expired += 1
                        _append_csv(output, {
                            "event_ms": now_ms, "signal_id": sig.signal_id, "latency_ms": profile.latency_ms,
                            "event": "expired_before_entry", "symbol": sig.symbol, "direction": sig.direction,
                            "signal_edge_bps": sig.edge_bps, "signal_spread_bps": sig.spread_bps,
                            "threshold_bps": sig.threshold_bps, "compute_us": sig.compute_us,
                        })
                        continue
                    snap = model.snapshot(now_ms=now_ms, threshold_bps=_required_edge(book.spread_bps, args))
                    same_edge = snap.ready and snap.direction == sig.direction
                    if not same_edge:
                        profile.expired += 1
                        _append_csv(output, {
                            "event_ms": now_ms, "signal_id": sig.signal_id, "latency_ms": profile.latency_ms,
                            "event": "expired_before_entry", "symbol": sig.symbol, "direction": sig.direction,
                            "signal_edge_bps": sig.edge_bps, "entry_edge_bps": snap.edge_bps,
                            "edge_lost_before_entry_bps": abs(sig.edge_bps) - abs(snap.edge_bps),
                            "signal_spread_bps": sig.spread_bps, "entry_spread_bps": book.spread_bps,
                            "threshold_bps": _required_edge(book.spread_bps, args), "compute_us": sig.compute_us,
                        })
                        continue
                    contract = contract_by_symbol[sig.symbol]
                    qty, vwap = _walk_depth(
                        book, direction=sig.direction, target_notional_usdt=args.target_notional_usdt,
                        contract_size=contract.contract_size, opening=True,
                    )
                    if qty <= 0 or vwap <= 0:
                        profile.no_depth += 1
                        continue
                    best = book.ask if sig.direction > 0 else book.bid
                    requested_qty = args.target_notional_usdt / best if best > 0 else 0.0
                    fill_ratio = min(1.0, qty / requested_qty) if requested_qty > 0 else 0.0
                    edge_loss = abs(sig.edge_bps) - abs(snap.edge_bps)
                    profile.edge_losses_bps.append(edge_loss)
                    profile.entries += 1
                    profile.position = SimPosition(
                        signal=sig, entry_ts_ms=now_ms, qty=qty, entry_price=vwap,
                        entry_edge_bps=float(snap.edge_bps), entry_spread_bps=float(book.spread_bps),
                        trailing=PositiveTrailing(distance_bps=max(args.trailing_distance_bps, book.spread_bps)),
                    )
                    _append_csv(output, {
                        "event_ms": now_ms, "signal_id": sig.signal_id, "latency_ms": profile.latency_ms,
                        "event": "virtual_entry", "symbol": sig.symbol, "direction": sig.direction,
                        "signal_edge_bps": sig.edge_bps, "entry_edge_bps": snap.edge_bps,
                        "edge_lost_before_entry_bps": edge_loss, "signal_spread_bps": sig.spread_bps,
                        "entry_spread_bps": book.spread_bps, "threshold_bps": _required_edge(book.spread_bps, args),
                        "compute_us": sig.compute_us, "entry_price": vwap, "qty": qty, "fill_ratio": fill_ratio,
                    })

            # Manage every virtual position with the same cross-exchange convergence idea.
            for profile in profiles.values():
                pos = profile.position
                if pos is None:
                    continue
                book = mexc.books.get(pos.signal.symbol)
                if book is None:
                    continue
                age_ms = now_ms - pos.entry_ts_ms
                contract = contract_by_symbol[pos.signal.symbol]
                filled, exit_vwap = _exit_depth_for_qty(
                    book, direction=pos.signal.direction, qty=pos.qty, contract_size=contract.contract_size,
                )
                if filled <= 0 or exit_vwap <= 0:
                    continue
                move_bps = _signed_move_bps(pos.signal.direction, pos.entry_price, exit_vwap)
                pos.mfe_bps = max(pos.mfe_bps, move_bps)
                pos.mae_bps = min(pos.mae_bps, move_bps)
                trail = pos.trailing.update(move_bps)
                snap = models[pos.signal.symbol].snapshot(now_ms=now_ms, threshold_bps=0.0)
                reason = None
                if trail is not None and move_bps <= trail and age_ms >= args.min_hold_ms:
                    reason = "positive_trailing_stop"
                if reason is None and move_bps <= -args.adverse_cut_bps and age_ms >= args.min_hold_ms:
                    reason = "adverse_cut"
                if reason is None and abs(snap.edge_bps) <= args.convergence_bps and age_ms >= args.min_hold_ms:
                    reason = "convergence"
                if reason is None and snap.direction not in (0, pos.signal.direction) and abs(snap.edge_bps) >= args.reversal_edge_bps and age_ms >= args.min_hold_ms:
                    reason = "residual_reversal"
                if reason is None and age_ms >= args.max_hold_ms:
                    reason = "max_hold"
                if reason is None:
                    continue
                # If depth is insufficient to flatten the whole virtual fill, keep waiting.
                if filled + 1e-12 < pos.qty:
                    continue
                pnl_bps = move_bps
                pnl_usdt = pos.qty * pos.entry_price * pnl_bps / 10000.0
                profile.pnl_bps += pnl_bps
                profile.pnl_usdt += pnl_usdt
                profile.holds_ms.append(float(age_ms))
                if pnl_bps > 0:
                    profile.wins += 1
                    profile.gross_win_bps += pnl_bps
                elif pnl_bps < 0:
                    profile.losses += 1
                    profile.gross_loss_bps += abs(pnl_bps)
                _append_csv(output, {
                    "event_ms": now_ms, "signal_id": pos.signal.signal_id, "latency_ms": profile.latency_ms,
                    "event": "virtual_exit", "symbol": pos.signal.symbol, "direction": pos.signal.direction,
                    "signal_edge_bps": pos.signal.edge_bps, "entry_edge_bps": pos.entry_edge_bps,
                    "signal_spread_bps": pos.signal.spread_bps, "entry_spread_bps": pos.entry_spread_bps,
                    "threshold_bps": pos.signal.threshold_bps, "compute_us": pos.signal.compute_us,
                    "entry_price": pos.entry_price, "exit_price": exit_vwap, "qty": pos.qty,
                    "hold_ms": age_ms, "pnl_bps": pnl_bps, "pnl_usdt": pnl_usdt,
                    "mfe_bps": pos.mfe_bps, "mae_bps": pos.mae_bps, "exit_reason": reason,
                })
                profile.position = None

            # One globally consumed crossing -> identical signal_id offered to every latency profile.
            if now >= warmup_until:
                candidates: list[Signal] = []
                for symbol in symbols:
                    if not fee_cache.fresh_zero(symbol, now_ms):
                        continue
                    book = mexc.books.get(symbol)
                    if book is None or now_ms - book.recv_ms > args.max_book_age_ms:
                        continue
                    sig = _candidate_signal(symbol, models[symbol], book, args, now_ms)
                    if sig is not None:
                        candidates.append(sig)
                if candidates:
                    sig = max(candidates, key=lambda row: abs(row.edge_bps) - row.threshold_bps)
                    signal_count += 1
                    for profile in profiles.values():
                        profile.signals_seen += 1
                        if profile.position is not None or profile.pending is not None:
                            profile.busy += 1
                            _append_csv(output, {
                                "event_ms": now_ms, "signal_id": sig.signal_id, "latency_ms": profile.latency_ms,
                                "event": "busy_skip", "symbol": sig.symbol, "direction": sig.direction,
                                "signal_edge_bps": sig.edge_bps, "signal_spread_bps": sig.spread_bps,
                                "threshold_bps": sig.threshold_bps, "compute_us": sig.compute_us,
                            })
                            continue
                        profile.pending = PendingEntry(sig, time.monotonic() + profile.latency_ms / 1000.0)
                    console.print(
                        f"SIGNAL #{signal_count} {sig.symbol} {'LONG' if sig.direction > 0 else 'SHORT'} "
                        f"edge={sig.edge_bps:+.3f}bps threshold={sig.threshold_bps:.3f} spread={sig.spread_bps:.3f} "
                        f"compute={sig.compute_us:.1f}us"
                    )

            if now >= next_heartbeat:
                console.print(
                    f"READ-ONLY heartbeat signals={signal_count}/{args.max_signals} Bquotes={binance.quotes} "
                    f"Mdepth={mexc.updates} books={len(mexc.books)}/{len(symbols)} fee_age={max(0, now_ms-fee_cache.checked_ms)}ms"
                )
                for profile in profiles.values():
                    console.print("  " + _summary_line(profile))
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

    console.print("\n[bold]FINAL SAME-SIGNAL LATENCY COMPARISON[/bold]")
    for profile in profiles.values():
        console.print(_summary_line(profile))
    console.print(f"Detailed per-signal comparison: {output.resolve()}")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Read-only same-signal Binance->MEXC latency diagnostic")
    p.add_argument("--session-seconds", type=float, default=1800.0)
    p.add_argument("--max-signals", type=int, default=300)
    p.add_argument("--latencies-ms", default="0,25,50,75,100,150,250")
    p.add_argument("--target-notional-usdt", type=float, default=10000.0)
    p.add_argument("--include-symbols", default="")
    p.add_argument("--exclude-symbols", default="")
    p.add_argument("--micro-horizon-ms", type=int, default=100)
    p.add_argument("--baseline-seconds", type=float, default=8.0)
    p.add_argument("--baseline-exclusion-ms", type=int, default=1000)
    p.add_argument("--min-edge-bps", type=float, default=0.35)
    p.add_argument("--min-net-edge-bps", type=float, default=0.20)
    p.add_argument("--edge-to-spread-ratio", type=float, default=1.15)
    p.add_argument("--min-binance-move-bps", type=float, default=0.05)
    p.add_argument("--max-binance-age-ms", type=float, default=300.0)
    p.add_argument("--max-mexc-age-ms", type=float, default=2000.0)
    p.add_argument("--max-book-age-ms", type=float, default=500.0)
    p.add_argument("--rearm-fraction", type=float, default=0.35)
    p.add_argument("--warmup-seconds", type=float, default=8.0)
    p.add_argument("--depth-limit", type=int, default=20)
    p.add_argument("--trailing-distance-bps", type=float, default=1.5)
    p.add_argument("--adverse-cut-bps", type=float, default=1.5)
    p.add_argument("--convergence-bps", type=float, default=0.10)
    p.add_argument("--reversal-edge-bps", type=float, default=0.20)
    p.add_argument("--min-hold-ms", type=int, default=50)
    p.add_argument("--max-hold-ms", type=int, default=15000)
    p.add_argument("--heartbeat-seconds", type=float, default=5.0)
    p.add_argument("--csv", default="")
    return p


def main() -> None:
    args = build_parser().parse_args()
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
