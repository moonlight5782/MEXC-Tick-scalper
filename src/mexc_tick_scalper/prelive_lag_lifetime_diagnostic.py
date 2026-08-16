from __future__ import annotations

import argparse
import asyncio
import csv
import math
import statistics
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from rich.console import Console

from .lead_lag_strategy import LeadLagGate, convergence_threshold
from .live_production_runner import FeeCache, _fee_loop
from .live_zero_fee_universe import LiveZeroFeeContract, discover_live_zero_fee_crosslisted
from .microspread import MicroSpreadModel
from .microspread_feed import EventBinanceBookTickerFeed, EventMexcDepthFeed
from .prelive_latency_diagnostic import _walk_depth
from .prelive_measured_rtt_diagnostic import _percentile, measure_live_private_rtt

console = Console()


@dataclass(slots=True)
class SignalTracker:
    signal_id: str
    symbol: str
    direction: int
    signal_ms: int
    signal_monotonic: float
    signal_residual_bps: float
    signal_threshold_bps: float
    signal_spread_bps: float
    signal_noise_bps: float
    signal_leader_advantage_bps: float
    measured_rtt_ms: float
    rtt_checked: bool = False
    rtt_survived: bool = False
    rtt_residual_bps: float | None = None
    rtt_spread_bps: float | None = None
    rtt_fill_ratio: float | None = None
    terminal_ms: int | None = None
    terminal_reason: str = ""
    terminal_residual_bps: float | None = None

    @property
    def lifetime_ms(self) -> float | None:
        if self.terminal_ms is None:
            return None
        return float(self.terminal_ms - self.signal_ms)


def _event_key(model: MicroSpreadModel) -> tuple[int, int] | None:
    if not model.binance or not model.mexc:
        return None
    return int(model.binance[-1][0]), int(model.mexc[-1][0])


def _valid_snapshot(snap) -> bool:
    return snap.reason not in {
        "warming_up", "warming_baseline", "warming_horizon",
        "stale_binance", "stale_mexc",
    }


def _residual_direction(edge_bps: float) -> int:
    return 1 if edge_bps > 0 else -1 if edge_bps < 0 else 0


def _terminal_reason(
    tracker: SignalTracker,
    *,
    residual_bps: float,
    convergence_bps: float,
    convergence_fraction: float,
    reversal_edge_bps: float,
    age_ms: float,
    max_track_ms: float,
) -> str | None:
    direction = _residual_direction(residual_bps)
    if direction == -tracker.direction and abs(residual_bps) >= reversal_edge_bps:
        return "residual_reversal"
    conv = convergence_threshold(
        tracker.signal_residual_bps,
        convergence_bps,
        convergence_fraction,
    )
    if abs(residual_bps) <= conv:
        return "convergence"
    if age_ms >= max_track_ms:
        return "track_timeout"
    return None


def _append(path: Path, row: dict[str, object]) -> None:
    fields = [
        "event_ms", "signal_id", "event", "symbol", "direction", "elapsed_ms",
        "measured_rtt_ms", "signal_residual_bps", "current_residual_bps",
        "signal_threshold_bps", "signal_noise_bps", "signal_spread_bps",
        "current_spread_bps", "leader_advantage_bps", "rtt_survived",
        "fill_ratio", "terminal_reason", "lifetime_ms",
    ]
    exists = path.exists()
    with path.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        if not exists:
            writer.writeheader()
        writer.writerow({name: row.get(name, "") for name in fields})


def _percent(v: int, total: int) -> float:
    return v / total * 100.0 if total else 0.0


def _median(values: list[float]) -> float:
    return statistics.median(values) if values else 0.0


def _summary(trackers: list[SignalTracker], measured_rtt_ms: float) -> str:
    checked = [t for t in trackers if t.rtt_checked]
    survived = [t for t in checked if t.rtt_survived]
    done = [t for t in trackers if t.terminal_ms is not None]
    lifetimes = [t.lifetime_ms for t in done if t.lifetime_ms is not None]
    convergence = sum(t.terminal_reason == "convergence" for t in done)
    reversal = sum(t.terminal_reason == "residual_reversal" for t in done)
    timeout = sum(t.terminal_reason == "track_timeout" for t in done)
    longer_than_rtt = sum((t.lifetime_ms or 0.0) >= measured_rtt_ms for t in done)
    return (
        f"signals={len(trackers)} rtt_checked={len(checked)} survived@RTT={len(survived)}/{len(checked)} "
        f"({_percent(len(survived), len(checked)):.1f}%) terminal={len(done)} "
        f"lifetime_med={_median([float(x) for x in lifetimes]):.1f}ms "
        f"lifetime>=RTT={longer_than_rtt}/{len(done)} ({_percent(longer_than_rtt, len(done)):.1f}%) "
        f"reasons conv/reversal/timeout={convergence}/{reversal}/{timeout}"
    )


async def run(args: argparse.Namespace) -> None:
    console.print("[cyan]PRE-LIVE INDEPENDENT LAG-LIFETIME DIAGNOSTIC[/cyan]")
    console.print("Phase 1: measuring current LIVE MEXC private-web RTT with read-only requests.")
    rtt_values = await measure_live_private_rtt(
        samples=args.rtt_samples,
        warmup_samples=args.rtt_warmup_samples,
        interval_ms=args.rtt_interval_ms,
    )
    rtt_median = statistics.median(rtt_values)
    rtt_p95 = _percentile(rtt_values, 0.95)
    measured_rtt_ms = rtt_p95 if args.rtt_profile == "p95" else rtt_median
    console.print(
        f"Measured RTT: median={rtt_median:.1f}ms p95={rtt_p95:.1f}ms "
        f"min={min(rtt_values):.1f}ms max={max(rtt_values):.1f}ms; "
        f"tracking checkpoint={args.rtt_profile}={measured_rtt_ms:.1f}ms"
    )

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
        row.mexc_symbol: MicroSpreadModel(
            horizon_ms=args.micro_horizon_ms,
            baseline_seconds=args.baseline_seconds,
            baseline_exclusion_ms=args.baseline_exclusion_ms,
            min_edge_bps=0.0,
            min_binance_move_bps=0.0,
            max_binance_age_ms=args.max_binance_age_ms,
            max_mexc_age_ms=args.max_mexc_age_ms,
        )
        for row in contracts
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
    fee_cache = FeeCache()
    fee_stop = asyncio.Event()
    fee_task = asyncio.create_task(_fee_loop(fee_cache, fee_stop))
    await binance.start()
    await mexc.start()

    output = Path(args.csv or f"prelive_lag_lifetime_{int(time.time())}.csv")
    deadline = time.monotonic() + args.session_seconds
    warmup_until = time.monotonic() + args.warmup_seconds
    trackers: list[SignalTracker] = []
    active: dict[str, SignalTracker] = {}
    next_heartbeat = 0.0
    signal_count = 0

    console.print(
        f"Phase 2: {len(symbols)} exact-0/0 symbols. Every signal tracked independently; "
        f"no busy suppression, no order writes. CSV: {output.resolve()}"
    )

    try:
        while time.monotonic() < deadline and (
            signal_count < args.max_signals or active
        ):
            now = time.monotonic()
            now_ms = int(time.time() * 1000)

            # Every signal gets its own lifetime. No position/pending profile can block another signal.
            for signal_id, tracker in list(active.items()):
                book = mexc.books.get(tracker.symbol)
                snap = models[tracker.symbol].snapshot(now_ms=now_ms, threshold_bps=0.0)
                age_ms = (now - tracker.signal_monotonic) * 1000.0

                if book is not None and _valid_snapshot(snap):
                    if not tracker.rtt_checked and age_ms >= measured_rtt_ms:
                        tracker.rtt_checked = True
                        decision = gate.assess(tracker.symbol, snap, book.spread_bps, now_ms)
                        survived = (
                            fee_cache.fresh_zero(tracker.symbol, now_ms)
                            and now_ms - book.recv_ms <= args.max_book_age_ms
                            and decision.ready
                            and decision.direction == tracker.direction
                        )
                        fill_ratio = 0.0
                        if survived:
                            contract = contract_by_symbol[tracker.symbol]
                            qty, _ = _walk_depth(
                                book,
                                direction=tracker.direction,
                                target_notional_usdt=args.target_notional_usdt,
                                contract_size=contract.contract_size,
                                opening=True,
                            )
                            best = book.ask if tracker.direction > 0 else book.bid
                            requested = args.target_notional_usdt / best if best > 0 else 0.0
                            fill_ratio = min(1.0, qty / requested) if requested > 0 else 0.0
                            survived = fill_ratio > 0.0
                        tracker.rtt_survived = survived
                        tracker.rtt_residual_bps = float(snap.edge_bps)
                        tracker.rtt_spread_bps = float(book.spread_bps)
                        tracker.rtt_fill_ratio = fill_ratio
                        _append(output, {
                            "event_ms": now_ms,
                            "signal_id": tracker.signal_id,
                            "event": "rtt_checkpoint",
                            "symbol": tracker.symbol,
                            "direction": tracker.direction,
                            "elapsed_ms": age_ms,
                            "measured_rtt_ms": measured_rtt_ms,
                            "signal_residual_bps": tracker.signal_residual_bps,
                            "current_residual_bps": snap.edge_bps,
                            "signal_threshold_bps": tracker.signal_threshold_bps,
                            "signal_noise_bps": tracker.signal_noise_bps,
                            "signal_spread_bps": tracker.signal_spread_bps,
                            "current_spread_bps": book.spread_bps,
                            "leader_advantage_bps": tracker.signal_leader_advantage_bps,
                            "rtt_survived": int(survived),
                            "fill_ratio": fill_ratio,
                        })

                    reason = _terminal_reason(
                        tracker,
                        residual_bps=float(snap.edge_bps),
                        convergence_bps=args.convergence_bps,
                        convergence_fraction=args.convergence_fraction,
                        reversal_edge_bps=args.reversal_edge_bps,
                        age_ms=age_ms,
                        max_track_ms=args.max_track_ms,
                    )
                else:
                    reason = "track_timeout" if age_ms >= args.max_track_ms else None

                if reason is not None:
                    tracker.terminal_ms = now_ms
                    tracker.terminal_reason = reason
                    tracker.terminal_residual_bps = float(snap.edge_bps) if _valid_snapshot(snap) else None
                    _append(output, {
                        "event_ms": now_ms,
                        "signal_id": tracker.signal_id,
                        "event": "terminal",
                        "symbol": tracker.symbol,
                        "direction": tracker.direction,
                        "elapsed_ms": age_ms,
                        "measured_rtt_ms": measured_rtt_ms,
                        "signal_residual_bps": tracker.signal_residual_bps,
                        "current_residual_bps": tracker.terminal_residual_bps,
                        "signal_threshold_bps": tracker.signal_threshold_bps,
                        "signal_noise_bps": tracker.signal_noise_bps,
                        "signal_spread_bps": tracker.signal_spread_bps,
                        "leader_advantage_bps": tracker.signal_leader_advantage_bps,
                        "rtt_survived": int(tracker.rtt_survived) if tracker.rtt_checked else "",
                        "fill_ratio": tracker.rtt_fill_ratio if tracker.rtt_checked else "",
                        "terminal_reason": reason,
                        "lifetime_ms": tracker.lifetime_ms,
                    })
                    active.pop(signal_id, None)

            if time.monotonic() >= warmup_until and signal_count < args.max_signals:
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
                    decision = gate.observe(
                        symbol,
                        snap,
                        book.spread_bps,
                        now_ms,
                        event_key=_event_key(model),
                    )
                    if decision.ready:
                        emitted.append((
                            abs(decision.residual_bps) - decision.threshold_bps,
                            -book.spread_bps,
                            symbol,
                            decision,
                            book,
                        ))
                if emitted:
                    _, _, symbol, decision, book = max(emitted, key=lambda row: (row[0], row[1]))
                    signal_count += 1
                    tracker = SignalTracker(
                        signal_id=uuid.uuid4().hex,
                        symbol=symbol,
                        direction=decision.direction,
                        signal_ms=now_ms,
                        signal_monotonic=time.monotonic(),
                        signal_residual_bps=float(decision.residual_bps),
                        signal_threshold_bps=float(decision.threshold_bps),
                        signal_spread_bps=float(book.spread_bps),
                        signal_noise_bps=float(decision.noise_bps),
                        signal_leader_advantage_bps=float(decision.leader_advantage_bps),
                        measured_rtt_ms=float(measured_rtt_ms),
                    )
                    trackers.append(tracker)
                    active[tracker.signal_id] = tracker
                    _append(output, {
                        "event_ms": now_ms,
                        "signal_id": tracker.signal_id,
                        "event": "signal",
                        "symbol": symbol,
                        "direction": decision.direction,
                        "elapsed_ms": 0.0,
                        "measured_rtt_ms": measured_rtt_ms,
                        "signal_residual_bps": decision.residual_bps,
                        "current_residual_bps": decision.residual_bps,
                        "signal_threshold_bps": decision.threshold_bps,
                        "signal_noise_bps": decision.noise_bps,
                        "signal_spread_bps": book.spread_bps,
                        "current_spread_bps": book.spread_bps,
                        "leader_advantage_bps": decision.leader_advantage_bps,
                    })
                    console.print(
                        f"SIGNAL #{signal_count} {symbol} {'LONG' if decision.direction > 0 else 'SHORT'} "
                        f"residual={decision.residual_bps:+.3f} threshold={decision.threshold_bps:.3f} "
                        f"noise={decision.noise_bps:.3f} Bmove={decision.binance_move_bps:+.3f} "
                        f"Mmove={decision.mexc_move_bps:+.3f} lead={decision.leader_advantage_bps:.3f}bps "
                        f"active={len(active)}"
                    )

            if now >= next_heartbeat:
                console.print(
                    f"LIFETIME heartbeat signals={signal_count}/{args.max_signals} active={len(active)} "
                    f"Bquotes={binance.quotes} Mdepth={mexc.updates} books={len(mexc.books)}/{len(symbols)}"
                )
                console.print("  " + _summary(trackers, measured_rtt_ms))
                next_heartbeat = now + args.heartbeat_seconds

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

    console.print("\n[bold]FINAL INDEPENDENT LAG-LIFETIME REPORT[/bold]")
    console.print(
        f"RTT median={rtt_median:.1f}ms p95={rtt_p95:.1f}ms selected={measured_rtt_ms:.1f}ms"
    )
    console.print(_summary(trackers, measured_rtt_ms))

    checked = [t for t in trackers if t.rtt_checked]
    survived = [t for t in checked if t.rtt_survived]
    lifetimes = [float(t.lifetime_ms) for t in trackers if t.lifetime_ms is not None]
    if lifetimes:
        console.print(
            f"Lifetime distribution: median={statistics.median(lifetimes):.1f}ms "
            f"p90={_percentile(lifetimes, 0.90):.1f}ms p95={_percentile(lifetimes, 0.95):.1f}ms "
            f"min={min(lifetimes):.1f}ms max={max(lifetimes):.1f}ms"
        )
    console.print(
        f"At measured RTT: survived={len(survived)}/{len(checked)} "
        f"({_percent(len(survived), len(checked)):.1f}%)."
    )
    console.print(f"Detailed signal timelines: {output.resolve()}")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Read-only independent Binance-lead/MEXC-lag lifetime diagnostic with measured LIVE private RTT"
    )
    p.add_argument("--session-seconds", type=float, default=1800.0)
    p.add_argument("--max-signals", type=int, default=300)
    p.add_argument("--target-notional-usdt", type=float, default=10000.0)
    p.add_argument("--include-symbols", default="")
    p.add_argument("--exclude-symbols", default="")
    p.add_argument("--rtt-samples", type=int, default=40)
    p.add_argument("--rtt-warmup-samples", type=int, default=3)
    p.add_argument("--rtt-interval-ms", type=float, default=100.0)
    p.add_argument("--rtt-profile", choices=("median", "p95"), default="median")
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
    p.add_argument("--convergence-bps", type=float, default=0.10)
    p.add_argument("--convergence-fraction", type=float, default=0.20)
    p.add_argument("--reversal-edge-bps", type=float, default=0.35)
    p.add_argument("--max-track-ms", type=float, default=5000.0)
    p.add_argument("--heartbeat-seconds", type=float, default=5.0)
    p.add_argument("--csv", default="")
    return p


def main() -> None:
    args = build_parser().parse_args()
    if args.rtt_samples <= 0:
        raise SystemExit("--rtt-samples must be positive")
    if args.max_signals <= 0:
        raise SystemExit("--max-signals must be positive")
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
