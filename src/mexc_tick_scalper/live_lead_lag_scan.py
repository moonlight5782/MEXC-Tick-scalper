from __future__ import annotations

import argparse
import asyncio
import csv
import json
import math
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import aiohttp
from dotenv import load_dotenv
from rich.console import Console
from rich.table import Table

from .lead_lag import BINANCE_FUTURES_WS, LeadLagModel
from .live_zero_fee_universe import LiveZeroFeeContract, discover_live_zero_fee_crosslisted
from .market import MexcPublicMarket

LIVE_REST = "https://contract.mexc.com"
LIVE_WS = "wss://contract.mexc.com/edge"
DEFAULT_SCAN_SECONDS = 60.0
DEFAULT_WARMUP_SECONDS = 8.0
DEFAULT_LOOP_SECONDS = 0.05
DEFAULT_EVENT_COOLDOWN_MS = 300
DEFAULT_MEXC_SHARD_SIZE = 20

console = Console()


@dataclass(slots=True)
class LeadLagStats:
    symbol: str
    binance_symbol: str
    events: int = 0
    long_events: int = 0
    short_events: int = 0
    sum_edge_bps: float = 0.0
    max_edge_bps: float = 0.0
    max_binance_move_bps: float = 0.0
    max_mexc_move_bps: float = 0.0
    min_age_ms: float = math.inf
    last_event_ms: int = -10**18
    live_spread_bps: float | None = None
    live_bid: float | None = None
    live_ask: float | None = None

    @property
    def avg_edge_bps(self) -> float:
        return self.sum_edge_bps / self.events if self.events else 0.0

    def record(self, *, direction: int, edge_bps: float, binance_move_bps: float, mexc_move_bps: float, age_ms: float, now_ms: int) -> None:
        edge = abs(float(edge_bps))
        self.events += 1
        if direction > 0:
            self.long_events += 1
        elif direction < 0:
            self.short_events += 1
        self.sum_edge_bps += edge
        self.max_edge_bps = max(self.max_edge_bps, edge)
        self.max_binance_move_bps = max(self.max_binance_move_bps, abs(float(binance_move_bps)))
        self.max_mexc_move_bps = max(self.max_mexc_move_bps, abs(float(mexc_move_bps)))
        self.min_age_ms = min(self.min_age_ms, float(age_ms))
        self.last_event_ms = int(now_ms)


def _chunks(items: list[str], size: int) -> list[list[str]]:
    size = max(1, int(size))
    return [items[i : i + size] for i in range(0, len(items), size)]


def _rank_key(row: LeadLagStats) -> tuple[float, float, float, float]:
    spread = row.live_spread_bps if row.live_spread_bps is not None else 1e9
    return (-row.events, -row.avg_edge_bps, -row.max_edge_bps, spread)


class MultiBinanceBookTickerFeed:
    """One Binance USD-M websocket carrying bookTicker for the whole research universe."""

    def __init__(self, contracts: list[LiveZeroFeeContract], models: dict[str, LeadLagModel]) -> None:
        self.contracts = contracts
        self.models = models
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self.last_error: str | None = None
        self.quotes = 0
        self.last_quote_ms = 0
        self._mexc_by_binance = {row.binance_symbol: row.mexc_symbol for row in contracts}

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
        timeout = aiohttp.ClientTimeout(total=None, sock_connect=10, sock_read=35)
        params = [f"{row.binance_symbol.lower()}@bookTicker" for row in self.contracts]
        while not self._stop.is_set():
            try:
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    async with session.ws_connect(BINANCE_FUTURES_WS, heartbeat=15) as ws:
                        await ws.send_json({"method": "SUBSCRIBE", "params": params, "id": 1})
                        self.last_error = None
                        async for msg in ws:
                            if self._stop.is_set():
                                return
                            if msg.type != aiohttp.WSMsgType.TEXT:
                                if msg.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSED):
                                    break
                                continue
                            payload = json.loads(msg.data)
                            if "result" in payload and payload.get("id") == 1:
                                continue
                            symbol = str(payload.get("s") or "").upper()
                            mexc_symbol = self._mexc_by_binance.get(symbol)
                            if mexc_symbol is None:
                                continue
                            bid = float(payload.get("b") or 0)
                            ask = float(payload.get("a") or 0)
                            if not (ask > bid > 0):
                                continue
                            now_ms = int(time.time() * 1000)
                            self.models[mexc_symbol].update_binance(bid=bid, ask=ask, ts_ms=now_ms)
                            self.quotes += 1
                            self.last_quote_ms = now_ms
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_error = f"{type(exc).__name__}: {exc}"
                await asyncio.sleep(0.5)


class MexcDealShardFeed:
    """Shard MEXC real-time deal subscriptions across several websocket connections."""

    def __init__(self, symbols: list[str], models: dict[str, LeadLagModel], *, shard_size: int = DEFAULT_MEXC_SHARD_SIZE) -> None:
        self.symbols = symbols
        self.models = models
        self.shard_size = max(1, int(shard_size))
        self._tasks: list[asyncio.Task] = []
        self._stop = asyncio.Event()
        self.last_errors: dict[int, str] = {}
        self.trades = 0
        self.last_trade_ms = 0

    async def start(self) -> None:
        self._stop.clear()
        for shard_id, rows in enumerate(_chunks(self.symbols, self.shard_size)):
            self._tasks.append(asyncio.create_task(self._run_shard(shard_id, rows)))

    async def close(self) -> None:
        self._stop.set()
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._tasks.clear()

    async def _run_shard(self, shard_id: int, symbols: list[str]) -> None:
        timeout = aiohttp.ClientTimeout(total=None, sock_connect=10, sock_read=30)
        while not self._stop.is_set():
            try:
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    async with session.ws_connect(LIVE_WS, heartbeat=None) as ws:
                        for symbol in symbols:
                            await ws.send_json({"method": "sub.deal", "param": {"symbol": symbol}, "gzip": False})
                        self.last_errors.pop(shard_id, None)
                        next_ping = time.monotonic() + 10.0
                        while not self._stop.is_set():
                            timeout_seconds = max(0.1, next_ping - time.monotonic())
                            try:
                                msg = await asyncio.wait_for(ws.receive(), timeout=timeout_seconds)
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

                            if payload.get("channel") != "push.deal":
                                continue
                            symbol = str(payload.get("symbol") or "").upper()
                            if symbol not in self.models:
                                continue
                            data = payload.get("data") or {}
                            rows = data if isinstance(data, list) else [data]
                            now_ms = int(time.time() * 1000)
                            for row in rows:
                                try:
                                    price = float(row.get("p") or 0)
                                except (TypeError, ValueError):
                                    continue
                                if price <= 0:
                                    continue
                                self.models[symbol].update_mexc_price(price=price, ts_ms=now_ms)
                                self.trades += 1
                                self.last_trade_ms = now_ms
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_errors[shard_id] = f"{type(exc).__name__}: {exc}"
                await asyncio.sleep(0.5)


async def _load_live_spreads(rows: list[LeadLagStats], *, limit: int) -> None:
    market = MexcPublicMarket(LIVE_REST, LIVE_WS)
    semaphore = asyncio.Semaphore(8)

    async def one(row: LeadLagStats) -> None:
        async with semaphore:
            try:
                ticker = await market.ticker(row.symbol)
            except Exception:
                return
        if ticker is None or not (ticker.ask > ticker.bid > 0):
            return
        mid = (ticker.ask + ticker.bid) / 2.0
        row.live_bid = ticker.bid
        row.live_ask = ticker.ask
        row.live_spread_bps = (ticker.ask - ticker.bid) / mid * 10_000.0

    await asyncio.gather(*(one(row) for row in rows[: max(1, int(limit))]))


def _write_csv(rows: Iterable[LeadLagStats], path: Path) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow([
            "symbol",
            "binance_symbol",
            "events",
            "long_events",
            "short_events",
            "avg_edge_bps",
            "max_edge_bps",
            "max_binance_move_bps",
            "max_mexc_move_bps",
            "min_age_ms",
            "live_spread_bps",
            "live_bid",
            "live_ask",
        ])
        for row in rows:
            writer.writerow([
                row.symbol,
                row.binance_symbol,
                row.events,
                row.long_events,
                row.short_events,
                f"{row.avg_edge_bps:.6f}",
                f"{row.max_edge_bps:.6f}",
                f"{row.max_binance_move_bps:.6f}",
                f"{row.max_mexc_move_bps:.6f}",
                "" if math.isinf(row.min_age_ms) else f"{row.min_age_ms:.1f}",
                "" if row.live_spread_bps is None else f"{row.live_spread_bps:.6f}",
                "" if row.live_bid is None else f"{row.live_bid:.12g}",
                "" if row.live_ask is None else f"{row.live_ask:.12g}",
            ])


async def run(args: argparse.Namespace) -> list[LeadLagStats]:
    root = Path(__file__).resolve().parents[2]
    load_dotenv(root / ".env", override=False)

    contracts = await discover_live_zero_fee_crosslisted()
    if not contracts:
        raise RuntimeError("no LIVE exact-zero-fee MEXC contracts cross-listed on Binance USD-M")

    console.print(
        f"[cyan]LIVE LEAD-LAG RESEARCH[/cyan]: {len(contracts)} exact 0/0-fee cross-listed pairs; "
        "market data only, LIVE writes disabled."
    )

    models = {
        row.mexc_symbol: LeadLagModel(
            horizon_ms=args.horizon_ms,
            baseline_seconds=args.baseline_seconds,
            min_edge_bps=args.min_edge_bps,
            min_binance_move_bps=args.min_binance_move_bps,
            max_age_ms=args.max_age_ms,
        )
        for row in contracts
    }
    stats = {
        row.mexc_symbol: LeadLagStats(row.mexc_symbol, row.binance_symbol)
        for row in contracts
    }

    binance = MultiBinanceBookTickerFeed(contracts, models)
    mexc = MexcDealShardFeed([row.mexc_symbol for row in contracts], models, shard_size=args.mexc_shard_size)
    await binance.start()
    await mexc.start()

    started = time.monotonic()
    warmup_until = started + args.warmup_seconds
    deadline = warmup_until + args.scan_seconds
    next_status = started

    try:
        while time.monotonic() < deadline:
            now = time.monotonic()
            now_ms = int(time.time() * 1000)

            if now >= warmup_until:
                for symbol, model in models.items():
                    snap = model.snapshot(now_ms=now_ms)
                    row = stats[symbol]
                    if not snap.ready:
                        continue
                    if now_ms - row.last_event_ms < args.event_cooldown_ms:
                        continue
                    row.record(
                        direction=snap.direction,
                        edge_bps=snap.edge_bps,
                        binance_move_bps=snap.binance_move_bps,
                        mexc_move_bps=snap.mexc_move_bps,
                        age_ms=snap.age_ms,
                        now_ms=now_ms,
                    )

            if now >= next_status:
                phase = "warmup" if now < warmup_until else "scan"
                elapsed_scan = max(0.0, now - warmup_until)
                total_events = sum(row.events for row in stats.values())
                console.print(
                    f"{phase.upper()} BinanceQuotes={binance.quotes} MEXCTrades={mexc.trades} "
                    f"events={total_events} scan_elapsed={elapsed_scan:.1f}s "
                    f"binance_error={binance.last_error or '-'} mexc_shard_errors={len(mexc.last_errors)}"
                )
                next_status = now + 5.0

            await asyncio.sleep(args.loop_seconds)
    finally:
        await binance.close()
        await mexc.close()

    rows = list(stats.values())
    rows.sort(key=_rank_key)
    await _load_live_spreads(rows, limit=args.spread_top)
    rows.sort(key=_rank_key)

    output_path = root / f"live_lead_lag_{int(time.time())}.csv"
    _write_csv(rows, output_path)

    table = Table(title="LIVE Binance -> MEXC lead-lag ranking (exact 0/0 fee universe)")
    table.add_column("#", justify="right")
    table.add_column("MEXC")
    table.add_column("Events", justify="right")
    table.add_column("L/S", justify="right")
    table.add_column("Avg edge", justify="right")
    table.add_column("Max edge", justify="right")
    table.add_column("Max B move", justify="right")
    table.add_column("LIVE spread", justify="right")
    for idx, row in enumerate(rows[: args.top], 1):
        spread = "-" if row.live_spread_bps is None else f"{row.live_spread_bps:.3f}bps"
        table.add_row(
            str(idx),
            row.symbol,
            str(row.events),
            f"{row.long_events}/{row.short_events}",
            f"{row.avg_edge_bps:.3f}bps",
            f"{row.max_edge_bps:.3f}bps",
            f"{row.max_binance_move_bps:.3f}bps",
            spread,
        )
    console.print(table)
    console.print(f"CSV saved: {output_path}")
    console.print("READ-ONLY COMPLETE: no LIVE order endpoint was enabled or called.")
    return rows


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Read-only multi-pair Binance->MEXC lead-lag scanner")
    parser.add_argument("--scan-seconds", type=float, default=DEFAULT_SCAN_SECONDS)
    parser.add_argument("--warmup-seconds", type=float, default=DEFAULT_WARMUP_SECONDS)
    parser.add_argument("--horizon-ms", type=int, default=250)
    parser.add_argument("--baseline-seconds", type=float, default=20.0)
    parser.add_argument("--min-edge-bps", type=float, default=4.0)
    parser.add_argument("--min-binance-move-bps", type=float, default=1.0)
    parser.add_argument("--max-age-ms", type=float, default=500.0)
    parser.add_argument("--event-cooldown-ms", type=int, default=DEFAULT_EVENT_COOLDOWN_MS)
    parser.add_argument("--mexc-shard-size", type=int, default=DEFAULT_MEXC_SHARD_SIZE)
    parser.add_argument("--loop-seconds", type=float, default=DEFAULT_LOOP_SECONDS)
    parser.add_argument("--spread-top", type=int, default=30)
    parser.add_argument("--top", type=int, default=20)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        console.print("[yellow]LIVE lead-lag research interrupted.[/yellow]")
    except Exception as exc:
        console.print(f"[red]LIVE LEAD-LAG SCAN FAILED:[/red] {type(exc).__name__}: {exc}")
        raise SystemExit(2) from exc


if __name__ == "__main__":
    main()
