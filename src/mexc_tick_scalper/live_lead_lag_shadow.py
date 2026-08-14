from __future__ import annotations

import argparse
import asyncio
import csv
import json
import math
import time
from dataclasses import dataclass, field
from pathlib import Path

import aiohttp
from dotenv import load_dotenv
from rich.console import Console
from rich.table import Table

from .lead_lag import LeadLagModel
from .live_lead_lag_scan import MultiBinanceBookTickerFeed, MexcDealShardFeed
from .live_zero_fee_universe import LiveZeroFeeContract, discover_live_zero_fee_crosslisted

LIVE_WS = "wss://contract.mexc.com/edge"
console = Console()


@dataclass(frozen=True, slots=True)
class BestBook:
    bid: float
    ask: float
    ts_ms: int

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2.0

    @property
    def spread_bps(self) -> float:
        mid = self.mid
        return (self.ask - self.bid) / mid * 10_000.0 if mid > 0 else math.inf


@dataclass(slots=True)
class PositiveTrailing:
    distance_bps: float
    peak_bps: float = 0.0
    stop_bps: float | None = None

    def update(self, move_bps: float) -> float | None:
        self.peak_bps = max(self.peak_bps, float(move_bps))
        candidate: float | None = None
        if self.peak_bps + 1e-9 >= 3.0:
            candidate = 0.5
        if self.peak_bps + 1e-9 >= 5.0:
            candidate = max(candidate or -math.inf, 2.0)
        if self.peak_bps + 1e-9 >= 6.0:
            candidate = max(candidate or -math.inf, self.peak_bps - max(0.1, self.distance_bps))
        if candidate is not None:
            self.stop_bps = candidate if self.stop_bps is None else max(self.stop_bps, candidate)
        return self.stop_bps


@dataclass(slots=True)
class ShadowPosition:
    symbol: str
    direction: int
    entry_price: float
    entry_edge_bps: float
    entry_spread_bps: float
    entry_ts: float
    trailing: PositiveTrailing
    mfe_bps: float = 0.0
    mae_bps: float = 0.0


@dataclass(frozen=True, slots=True)
class ShadowTrade:
    symbol: str
    direction: int
    entry_price: float
    exit_price: float
    entry_edge_bps: float
    entry_spread_bps: float
    pnl_bps: float
    pnl_usdt: float
    mfe_bps: float
    mae_bps: float
    duration_s: float
    exit_reason: str
    exit_trail_bps: float | None


@dataclass(slots=True)
class SymbolShadowStats:
    symbol: str
    trades: int = 0
    wins: int = 0
    losses: int = 0
    pnl_bps: float = 0.0
    pnl_usdt: float = 0.0
    gross_win_bps: float = 0.0
    gross_loss_bps: float = 0.0
    max_mfe_bps: float = 0.0
    max_mae_bps: float = 0.0
    durations: list[float] = field(default_factory=list)

    def add(self, trade: ShadowTrade) -> None:
        self.trades += 1
        self.pnl_bps += trade.pnl_bps
        self.pnl_usdt += trade.pnl_usdt
        self.max_mfe_bps = max(self.max_mfe_bps, trade.mfe_bps)
        self.max_mae_bps = min(self.max_mae_bps, trade.mae_bps)
        self.durations.append(trade.duration_s)
        if trade.pnl_bps > 0:
            self.wins += 1
            self.gross_win_bps += trade.pnl_bps
        elif trade.pnl_bps < 0:
            self.losses += 1
            self.gross_loss_bps += abs(trade.pnl_bps)

    @property
    def win_rate(self) -> float:
        return self.wins / self.trades if self.trades else 0.0

    @property
    def profit_factor(self) -> float:
        if self.gross_loss_bps == 0:
            return math.inf if self.gross_win_bps > 0 else 0.0
        return self.gross_win_bps / self.gross_loss_bps

    @property
    def avg_hold(self) -> float:
        return sum(self.durations) / len(self.durations) if self.durations else 0.0


def _signed_move_bps(direction: int, entry: float, current: float) -> float:
    if entry <= 0 or current <= 0 or direction not in (-1, 1):
        return 0.0
    return direction * (current - entry) / entry * 10_000.0


def _candidate_score(events: int, avg_edge_bps: float, spread_bps: float) -> float:
    net = avg_edge_bps - spread_bps
    return net * math.sqrt(max(1, events))


def _latest_scan_csv(root: Path) -> Path | None:
    rows = sorted(root.glob("live_lead_lag_*.csv"), key=lambda p: p.stat().st_mtime, reverse=True)
    return rows[0] if rows else None


def _load_candidates_from_csv(
    path: Path,
    *,
    available: set[str],
    top: int,
    min_events: int,
    min_net_edge_bps: float,
) -> list[tuple[str, int, float, float, float]]:
    ranked: list[tuple[str, int, float, float, float]] = []
    with path.open("r", newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            symbol = str(row.get("symbol") or "").upper()
            if symbol not in available:
                continue
            try:
                events = int(row.get("events") or 0)
                avg_edge = float(row.get("avg_edge_bps") or 0)
                spread = float(row.get("live_spread_bps") or "nan")
            except (TypeError, ValueError):
                continue
            if events < min_events or not math.isfinite(spread):
                continue
            net = avg_edge - spread
            if net < min_net_edge_bps:
                continue
            score = _candidate_score(events, avg_edge, spread)
            ranked.append((symbol, events, avg_edge, spread, score))
    ranked.sort(key=lambda item: item[4], reverse=True)
    return ranked[: max(1, int(top))]


class MexcBestBookFeed:
    """Real-time MEXC Futures best bid/ask using full depth websocket pushes."""

    def __init__(self, symbols: list[str]) -> None:
        self.symbols = symbols
        self.books: dict[str, BestBook] = {}
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self.last_error: str | None = None
        self.updates = 0

    @staticmethod
    def parse_payload(payload: dict) -> tuple[str, BestBook] | None:
        channel = str(payload.get("channel") or "")
        if not channel.startswith("push.depth"):
            return None
        symbol = str(payload.get("symbol") or "").upper()
        data = payload.get("data") or {}
        if not symbol or not isinstance(data, dict):
            return None
        bids = data.get("bids") or []
        asks = data.get("asks") or []
        try:
            bid = max(float(row[0]) for row in bids if isinstance(row, (list, tuple)) and len(row) >= 2 and float(row[1]) > 0)
            ask = min(float(row[0]) for row in asks if isinstance(row, (list, tuple)) and len(row) >= 2 and float(row[1]) > 0)
        except (ValueError, TypeError):
            return None
        if not (ask > bid > 0):
            return None
        ts_ms = int(payload.get("ts") or data.get("timestamp") or time.time() * 1000)
        if ts_ms < 10_000_000_000:
            ts_ms *= 1000
        return symbol, BestBook(bid=bid, ask=ask, ts_ms=ts_ms)

    async def start(self) -> None:
        self._stop.clear()
        self._task = asyncio.create_task(self._run())

    async def close(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _run(self) -> None:
        timeout = aiohttp.ClientTimeout(total=None, sock_connect=10, sock_read=30)
        while not self._stop.is_set():
            try:
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    async with session.ws_connect(LIVE_WS, heartbeat=None) as ws:
                        for symbol in self.symbols:
                            await ws.send_json({
                                "method": "sub.depth.full",
                                "param": {"symbol": symbol, "limit": 5},
                                "gzip": False,
                            })
                        self.last_error = None
                        next_ping = time.monotonic() + 10.0
                        while not self._stop.is_set():
                            timeout_s = max(0.1, next_ping - time.monotonic())
                            try:
                                msg = await asyncio.wait_for(ws.receive(), timeout=timeout_s)
                            except TimeoutError:
                                await ws.send_json({"method": "ping"})
                                next_ping = time.monotonic() + 10.0
                                continue
                            if time.monotonic() >= next_ping:
                                await ws.send_json({"method": "ping"})
                                next_ping = time.monotonic() + 10.0
                            if msg.type == aiohttp.WSMsgType.TEXT:
                                payload = json.loads(msg.data)
                            elif msg.type == aiohttp.WSMsgType.BINARY:
                                payload = json.loads(msg.data.decode("utf-8"))
                            elif msg.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSED):
                                break
                            else:
                                continue
                            parsed = self.parse_payload(payload)
                            if parsed is None:
                                continue
                            symbol, book = parsed
                            if symbol not in self.symbols:
                                continue
                            self.books[symbol] = book
                            self.updates += 1
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_error = f"{type(exc).__name__}: {exc}"
                await asyncio.sleep(0.5)


def _write_trades(path: Path, trades: list[ShadowTrade]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow([
            "symbol", "direction", "entry_price", "exit_price", "entry_edge_bps",
            "entry_spread_bps", "pnl_bps", "pnl_usdt", "mfe_bps", "mae_bps",
            "duration_s", "exit_reason", "exit_trail_bps",
        ])
        for row in trades:
            writer.writerow([
                row.symbol,
                "LONG" if row.direction > 0 else "SHORT",
                f"{row.entry_price:.12g}",
                f"{row.exit_price:.12g}",
                f"{row.entry_edge_bps:.6f}",
                f"{row.entry_spread_bps:.6f}",
                f"{row.pnl_bps:.6f}",
                f"{row.pnl_usdt:.8f}",
                f"{row.mfe_bps:.6f}",
                f"{row.mae_bps:.6f}",
                f"{row.duration_s:.6f}",
                row.exit_reason,
                "" if row.exit_trail_bps is None else f"{row.exit_trail_bps:.6f}",
            ])


async def run(args: argparse.Namespace) -> list[ShadowTrade]:
    root = Path(__file__).resolve().parents[2]
    load_dotenv(root / ".env", override=False)

    universe = await discover_live_zero_fee_crosslisted()
    by_symbol: dict[str, LiveZeroFeeContract] = {row.mexc_symbol: row for row in universe}
    scan_path = Path(args.scan_csv) if args.scan_csv else _latest_scan_csv(root)
    if scan_path is None or not scan_path.exists():
        raise RuntimeError("no live_lead_lag_*.csv found; run live_lead_lag_scan first")

    ranked = _load_candidates_from_csv(
        scan_path,
        available=set(by_symbol),
        top=args.top,
        min_events=args.min_events,
        min_net_edge_bps=args.min_net_edge_bps,
    )
    if not ranked:
        raise RuntimeError("latest scan has no candidate satisfying event/net-edge requirements")

    symbols = [row[0] for row in ranked]
    contracts = [by_symbol[symbol] for symbol in symbols]

    table = Table(title=f"Shadow candidates from {scan_path.name}")
    table.add_column("#", justify="right")
    table.add_column("MEXC")
    table.add_column("Events", justify="right")
    table.add_column("Avg edge", justify="right")
    table.add_column("Spread", justify="right")
    table.add_column("Net", justify="right")
    table.add_column("Score", justify="right")
    for idx, (symbol, events, avg_edge, spread, score) in enumerate(ranked, 1):
        table.add_row(str(idx), symbol, str(events), f"{avg_edge:.3f}", f"{spread:.3f}", f"{avg_edge-spread:.3f}", f"{score:.2f}")
    console.print(table)
    console.print(
        "[cyan]LIVE SHADOW ONLY[/cyan]: exact 0/0-fee universe, LIVE market data, executable MEXC bid/ask; "
        "no LIVE order adapter is constructed and no order endpoint can be called."
    )

    models = {
        symbol: LeadLagModel(
            horizon_ms=args.horizon_ms,
            baseline_seconds=args.baseline_seconds,
            min_edge_bps=args.min_edge_bps,
            min_binance_move_bps=args.min_binance_move_bps,
            max_age_ms=args.max_age_ms,
        )
        for symbol in symbols
    }
    binance = MultiBinanceBookTickerFeed(contracts, models)
    mexc_trades = MexcDealShardFeed(symbols, models, shard_size=max(1, len(symbols)))
    books = MexcBestBookFeed(symbols)

    await binance.start()
    await mexc_trades.start()
    await books.start()

    positions: dict[str, ShadowPosition] = {}
    closed: list[ShadowTrade] = []
    stats = {symbol: SymbolShadowStats(symbol) for symbol in symbols}
    last_entry_ms = {symbol: -10**18 for symbol in symbols}

    started = time.monotonic()
    warmup_until = started + args.warmup_seconds
    deadline = warmup_until + args.seconds
    next_status = started

    def close_shadow(symbol: str, pos: ShadowPosition, exit_price: float, reason: str, now: float) -> None:
        move_bps = _signed_move_bps(pos.direction, pos.entry_price, exit_price)
        trade = ShadowTrade(
            symbol=symbol,
            direction=pos.direction,
            entry_price=pos.entry_price,
            exit_price=exit_price,
            entry_edge_bps=pos.entry_edge_bps,
            entry_spread_bps=pos.entry_spread_bps,
            pnl_bps=move_bps,
            pnl_usdt=args.notional_usdt * move_bps / 10_000.0,
            mfe_bps=pos.mfe_bps,
            mae_bps=pos.mae_bps,
            duration_s=now - pos.entry_ts,
            exit_reason=reason,
            exit_trail_bps=pos.trailing.stop_bps,
        )
        closed.append(trade)
        stats[symbol].add(trade)
        last_entry_ms[symbol] = int(time.time() * 1000)
        trail_text = "OFF" if pos.trailing.stop_bps is None else f"+{pos.trailing.stop_bps:.3f}bps"
        console.print(
            f"SHADOW EXIT {symbol} {'LONG' if pos.direction > 0 else 'SHORT'} reason={reason} "
            f"pnl={move_bps:+.3f}bps MFE={pos.mfe_bps:+.3f} MAE={pos.mae_bps:+.3f} "
            f"trail={trail_text} hold={trade.duration_s:.2f}s"
        )
        positions.pop(symbol, None)

    try:
        while time.monotonic() < deadline:
            now = time.monotonic()
            now_ms = int(time.time() * 1000)

            for symbol in symbols:
                model = models[symbol]
                snap = model.snapshot(now_ms=now_ms)
                book = books.books.get(symbol)
                if book is None:
                    continue
                book_age = now_ms - book.ts_ms
                if book_age < 0 or book_age > args.max_book_age_ms:
                    continue

                pos = positions.get(symbol)
                if pos is None:
                    if now < warmup_until or not snap.ready:
                        continue
                    if now_ms - last_entry_ms[symbol] < args.entry_cooldown_ms:
                        continue
                    spread_bps = book.spread_bps
                    required_edge = max(args.min_edge_bps, spread_bps + args.min_net_edge_bps)
                    if abs(snap.edge_bps) < required_edge:
                        continue
                    direction = snap.direction
                    entry_price = book.ask if direction > 0 else book.bid
                    positions[symbol] = ShadowPosition(
                        symbol=symbol,
                        direction=direction,
                        entry_price=entry_price,
                        entry_edge_bps=abs(snap.edge_bps),
                        entry_spread_bps=spread_bps,
                        entry_ts=now,
                        trailing=PositiveTrailing(distance_bps=max(args.trailing_distance_bps, spread_bps)),
                    )
                    console.print(
                        f"SHADOW ENTRY {symbol} {'LONG' if direction > 0 else 'SHORT'} "
                        f"edge={snap.edge_bps:+.3f}bps required={required_edge:.3f} "
                        f"spread={spread_bps:.3f} Bmove={snap.binance_move_bps:+.3f} "
                        f"Mmove={snap.mexc_move_bps:+.3f} px={entry_price:g}"
                    )
                    continue

                exit_price = book.bid if pos.direction > 0 else book.ask
                move_bps = _signed_move_bps(pos.direction, pos.entry_price, exit_price)
                pos.mfe_bps = max(pos.mfe_bps, move_bps)
                pos.mae_bps = min(pos.mae_bps, move_bps)
                trail = pos.trailing.update(move_bps)
                age_s = now - pos.entry_ts

                reason: str | None = None
                if trail is not None and move_bps <= trail and age_s >= args.min_hold_seconds:
                    reason = "positive_trailing_stop"
                adverse_limit = max(args.adverse_cut_bps, pos.entry_spread_bps * args.adverse_spread_mult)
                if reason is None and move_bps <= -adverse_limit and age_s >= args.min_hold_seconds:
                    reason = "adverse_cut"
                convergence = max(args.convergence_bps, pos.entry_edge_bps * args.convergence_fraction)
                if reason is None and snap.age_ms <= args.max_age_ms and abs(snap.edge_bps) <= convergence:
                    reason = "lead_lag_converged"
                if (
                    reason is None
                    and snap.age_ms <= args.max_age_ms
                    and snap.direction == -pos.direction
                    and abs(snap.edge_bps) >= args.reversal_edge_bps
                ):
                    reason = "lead_lag_reversed"
                if (
                    reason is None
                    and snap.age_ms <= args.max_age_ms
                    and snap.binance_move_bps * pos.direction <= -args.min_binance_move_bps
                ):
                    reason = "binance_reversal"
                if reason is None and age_s >= args.max_hold_seconds:
                    reason = "timeout"

                if reason is not None:
                    close_shadow(symbol, pos, exit_price, reason, now)

            if now >= next_status:
                total_pnl = sum(row.pnl_bps for row in stats.values())
                console.print(
                    f"SHADOW STATUS books={len(books.books)}/{len(symbols)} depth_updates={books.updates} "
                    f"BinanceQuotes={binance.quotes} MEXCTrades={mexc_trades.trades} "
                    f"open={len(positions)} closed={len(closed)} pnl={total_pnl:+.3f}bps "
                    f"book_error={books.last_error or '-'}"
                )
                for symbol, pos in positions.items():
                    trail = pos.trailing.stop_bps
                    trail_txt = "OFF" if trail is None else f"+{trail:.3f}bps"
                    console.print(
                        f"  {symbol} MFE={pos.mfe_bps:+.3f} MAE={pos.mae_bps:+.3f} "
                        f"TRAIL={trail_txt} edge_entry={pos.entry_edge_bps:.3f}bps"
                    )
                next_status = now + args.status_seconds

            await asyncio.sleep(args.loop_seconds)
    finally:
        now = time.monotonic()
        for symbol, pos in list(positions.items()):
            book = books.books.get(symbol)
            if book is not None:
                exit_price = book.bid if pos.direction > 0 else book.ask
                close_shadow(symbol, pos, exit_price, "session_end", now)
        await binance.close()
        await mexc_trades.close()
        await books.close()

    output = root / f"live_shadow_trades_{int(time.time())}.csv"
    _write_trades(output, closed)

    summary = sorted(stats.values(), key=lambda row: (row.pnl_bps, row.trades), reverse=True)
    result = Table(title="LIVE lead-lag SHADOW results (0 fees assumed only because LIVE account currently reports 0/0)")
    result.add_column("#", justify="right")
    result.add_column("MEXC")
    result.add_column("Trades", justify="right")
    result.add_column("W/L", justify="right")
    result.add_column("Win%", justify="right")
    result.add_column("PnL bps", justify="right")
    result.add_column("PnL $100", justify="right")
    result.add_column("PF", justify="right")
    result.add_column("Avg hold", justify="right")
    for idx, row in enumerate(summary, 1):
        pf = "inf" if math.isinf(row.profit_factor) else f"{row.profit_factor:.2f}"
        result.add_row(
            str(idx), row.symbol, str(row.trades), f"{row.wins}/{row.losses}", f"{row.win_rate*100:.1f}%",
            f"{row.pnl_bps:+.3f}", f"{row.pnl_usdt:+.4f}", pf, f"{row.avg_hold:.2f}s",
        )
    console.print(result)
    console.print(f"Trade CSV saved: {output}")
    console.print("READ-ONLY SHADOW COMPLETE: no LIVE order endpoint was enabled or called.")
    return closed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="LIVE Binance->MEXC shadow trader with zero real orders")
    parser.add_argument("--scan-csv", default="")
    parser.add_argument("--seconds", type=float, default=300.0)
    parser.add_argument("--warmup-seconds", type=float, default=8.0)
    parser.add_argument("--top", type=int, default=6)
    parser.add_argument("--min-events", type=int, default=2)
    parser.add_argument("--min-net-edge-bps", type=float, default=2.0)
    parser.add_argument("--min-edge-bps", type=float, default=4.0)
    parser.add_argument("--min-binance-move-bps", type=float, default=1.0)
    parser.add_argument("--horizon-ms", type=int, default=250)
    parser.add_argument("--baseline-seconds", type=float, default=20.0)
    parser.add_argument("--max-age-ms", type=float, default=500.0)
    parser.add_argument("--max-book-age-ms", type=float, default=750.0)
    parser.add_argument("--entry-cooldown-ms", type=int, default=500)
    parser.add_argument("--loop-seconds", type=float, default=0.05)
    parser.add_argument("--status-seconds", type=float, default=5.0)
    parser.add_argument("--notional-usdt", type=float, default=100.0)
    parser.add_argument("--min-hold-seconds", type=float, default=0.15)
    parser.add_argument("--max-hold-seconds", type=float, default=60.0)
    parser.add_argument("--adverse-cut-bps", type=float, default=4.0)
    parser.add_argument("--adverse-spread-mult", type=float, default=1.25)
    parser.add_argument("--convergence-bps", type=float, default=1.0)
    parser.add_argument("--convergence-fraction", type=float, default=0.25)
    parser.add_argument("--reversal-edge-bps", type=float, default=2.0)
    parser.add_argument("--trailing-distance-bps", type=float, default=1.5)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        console.print("[yellow]Shadow scan stopped by user.[/yellow]")
    except Exception as exc:
        console.print(f"[red]LIVE SHADOW FAILED:[/red] {type(exc).__name__}: {exc}")
        raise SystemExit(2) from exc


if __name__ == "__main__":
    main()
