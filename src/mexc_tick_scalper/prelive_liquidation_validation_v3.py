from __future__ import annotations

import asyncio
import csv
import json
import time
from pathlib import Path

import aiohttp
from rich.console import Console

from . import prelive_liquidation_validation as base
from . import prelive_liquidation_validation_v2 as bank
from . import prelive_persistent_ioc_shadow_v2 as core

console = Console()
MEXC_WS = "wss://contract.mexc.com/edge"


async def _ticker_fair_price_loop(symbols: list[str], stop: asyncio.Event, rows: list[base.FairTick], csv_path: Path) -> None:
    """Reliable once-per-second MEXC fair-price trace via public tickers stream."""
    wanted = set(symbols)
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    with csv_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["ts_ms", "symbol", "fair_price"])
        while not stop.is_set():
            try:
                timeout = aiohttp.ClientTimeout(total=None, sock_connect=15, sock_read=None)
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    async with session.ws_connect(MEXC_WS, heartbeat=20) as ws:
                        await ws.send_json({"method": "sub.tickers", "param": {}, "gzip": False})
                        last_ping = time.monotonic()
                        while not stop.is_set():
                            if time.monotonic() - last_ping >= 10.0:
                                await ws.send_json({"method": "ping"})
                                last_ping = time.monotonic()
                            try:
                                msg = await asyncio.wait_for(ws.receive(), timeout=1.5)
                            except TimeoutError:
                                continue
                            if msg.type == aiohttp.WSMsgType.TEXT:
                                payload = json.loads(msg.data)
                                if payload.get("channel") != "push.tickers":
                                    continue
                                ts_ms = int(payload.get("ts") or time.time() * 1000)
                                data = payload.get("data") or []
                                if isinstance(data, dict):
                                    data = [data]
                                for item in data:
                                    symbol = str(item.get("symbol") or "").upper()
                                    if symbol not in wanted:
                                        continue
                                    try:
                                        price = float(item.get("fairPrice") or 0.0)
                                    except (TypeError, ValueError):
                                        continue
                                    if price <= 0:
                                        continue
                                    tick = base.FairTick(ts_ms=ts_ms, symbol=symbol, price=price)
                                    rows.append(tick)
                                    writer.writerow([tick.ts_ms, tick.symbol, f"{tick.price:.12g}"])
                                fh.flush()
                            elif msg.type in {aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.ERROR}:
                                break
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                console.print(f"[yellow]Ticker fair-price WS reconnect after error:[/yellow] {exc}")
                try:
                    await asyncio.wait_for(stop.wait(), timeout=1.0)
                except TimeoutError:
                    pass


async def run(args) -> None:
    original_fair_loop = base._fair_price_loop
    original_cost = core.immediate_roundtrip_cost_bps

    async def fair_loop(symbols, stop, rows, csv_path):
        await _ticker_fair_price_loop(symbols, stop, rows, csv_path)

    def guarded_cost(book, *, direction: int, entry_price: float, qty: float, contract_size: float) -> float:
        # This is a market-data/execution sanity guard, not a strategy-alpha change.
        if book.spread_bps > args.max_arrival_spread_bps:
            return float("inf")
        cost = original_cost(
            book,
            direction=direction,
            entry_price=entry_price,
            qty=qty,
            contract_size=contract_size,
        )
        if cost > args.max_roundtrip_cost_bps:
            return float("inf")
        return cost

    base._fair_price_loop = fair_loop
    core.immediate_roundtrip_cost_bps = guarded_cost
    console.print(
        f"[bold cyan]V3 SANITY GUARDS[/bold cyan] max_arrival_spread={args.max_arrival_spread_bps:.1f}bps "
        f"max_roundtrip_cost={args.max_roundtrip_cost_bps:.1f}bps; fair price=ticker stream"
    )
    try:
        await bank.run(args)
    finally:
        base._fair_price_loop = original_fair_loop
        core.immediate_roundtrip_cost_bps = original_cost


def main() -> None:
    p = base.build_parser()
    p.description = "Bank-aware baseline-v1 LIVE paper liquidation validation with market sanity guards"
    p.add_argument("--max-arrival-spread-bps", type=float, default=20.0)
    p.add_argument("--max-roundtrip-cost-bps", type=float, default=25.0)
    args = p.parse_args()
    if args.balance_usdt <= 0:
        raise SystemExit("--balance-usdt must be > 0")
    if not 0 < args.margin_fraction <= 1:
        raise SystemExit("--margin-fraction must be in (0, 1]")
    if args.max_arrival_spread_bps <= 0 or args.max_roundtrip_cost_bps <= 0:
        raise SystemExit("sanity guard thresholds must be > 0")
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
