from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from rich.console import Console
from rich.table import Table

from .demo_discovery import _fetch_contracts
from .execution import OrderSide
from .market import MexcPublicMarket
from .web_execution import MexcWebError, MexcWebExecutionAdapter, WebExecutionConfig
from .web_fee import provider_from_web_fee_payload

console = Console()
LIVE_REST = "https://contract.mexc.com"
LIVE_WS = "wss://contract.mexc.com/edge"


@dataclass(slots=True)
class LiveSample:
    symbol: str
    ticks: int
    price_changes: int
    duration: float

    @property
    def trade_rate(self) -> float:
        return self.ticks / self.duration if self.duration > 0 else 0.0

    @property
    def change_rate(self) -> float:
        return self.price_changes / self.duration if self.duration > 0 else 0.0


def _load_env() -> None:
    root = Path(__file__).resolve().parents[2]
    load_dotenv(root / ".env", override=False)


async def _sample_live(symbol: str, seconds: float = 8.0) -> LiveSample:
    market = MexcPublicMarket(LIVE_REST, LIVE_WS)
    start = time.monotonic()
    ticks = 0
    changes = 0
    last_price: float | None = None

    async def collect() -> None:
        nonlocal ticks, changes, last_price
        async for tick in market.trades(symbol):
            ticks += 1
            if last_price is not None and tick.price != last_price:
                changes += 1
            last_price = tick.price
            if time.monotonic() - start >= seconds:
                break

    try:
        await asyncio.wait_for(collect(), timeout=seconds + 2.0)
    except TimeoutError:
        pass
    return LiveSample(symbol, ticks, changes, max(0.001, time.monotonic() - start))


async def _candidates() -> list[dict[str, Any]]:
    cfg = WebExecutionConfig.demo_from_env(write_enabled=False)
    async with MexcWebExecutionAdapter(cfg) as adapter:
        contracts = await _fetch_contracts(adapter)
        fees = provider_from_web_fee_payload(await adapter.get_fee_rates())
        rows: list[dict[str, Any]] = []
        for row in contracts:
            symbol = str(row.get("symbol", "")).upper()
            status = fees.status(symbol)
            if status.maker != 0 or status.taker != 0:
                continue
            try:
                ask = await adapter.get_best_price(symbol, OrderSide.LONG)
                bid = await adapter.get_best_price(symbol, OrderSide.SHORT)
            except MexcWebError:
                continue
            if ask <= 0 or bid <= 0 or ask < bid:
                continue
            item = dict(row)
            item["spreadPct"] = ((ask - bid) / ((ask + bid) / 2.0)) * 100.0
            rows.append(item)

    console.print(f"Measuring LIVE MEXC activity for {len(rows)} zero-fee Demo-compatible pairs...")
    samples = await asyncio.gather(*(_sample_live(str(r.get("symbol", "")).upper()) for r in rows))
    sample_map = {s.symbol: s for s in samples}
    for row in rows:
        s = sample_map[str(row.get("symbol", "")).upper()]
        row["liveTicks"] = s.ticks
        row["liveTradeRate"] = s.trade_rate
        row["liveChangeRate"] = s.change_rate
    rows.sort(key=lambda r: (-float(r.get("liveChangeRate") or 0), -float(r.get("liveTradeRate") or 0), float(r.get("spreadPct") or 999)))
    return rows


def _show(rows: list[dict[str, Any]]) -> None:
    table = Table(title="Zero-fee Demo pairs ranked by LIVE MEXC tape")
    table.add_column("#", justify="right")
    table.add_column("Symbol")
    table.add_column("LIVE chg/s", justify="right")
    table.add_column("LIVE trades/s", justify="right")
    table.add_column("Demo spread %", justify="right")
    table.add_column("Max lev", justify="right")
    for i, row in enumerate(rows, 1):
        table.add_row(str(i), str(row.get("symbol", "?")), f"{float(row.get('liveChangeRate') or 0):.2f}", f"{float(row.get('liveTradeRate') or 0):.2f}", f"{float(row.get('spreadPct') or 0):.4f}", str(row.get("maxLeverage", "?")))
    console.print(table)


def _ask_int(prompt: str, default: int) -> int:
    raw = input(f"{prompt} [{default}]: ").strip()
    return default if not raw else int(raw)


def _ask_float(prompt: str, default: float) -> float:
    raw = input(f"{prompt} [{default:g}]: ").strip()
    return default if not raw else float(raw)


async def main_async() -> None:
    _load_env()
    rows = await _candidates()
    if not rows:
        raise MexcWebError("no confirmed zero-fee Demo-compatible contracts")
    _show(rows)
    active = [r for r in rows if float(r.get("liveChangeRate") or 0) > 0]
    if not active:
        raise MexcWebError("none of the zero-fee Demo-compatible pairs changed LIVE trade price during the sample")

    choice = input("Select pair number (or A for automatic most-active LIVE pair): ").strip().lower()
    selected = active[0] if choice in {"", "a", "auto"} else rows[int(choice) - 1]
    if float(selected.get("liveChangeRate") or 0) <= 0:
        raise MexcWebError(f"{selected.get('symbol')} had no LIVE trade-price changes; choose an active pair")

    symbol = str(selected.get("symbol", "")).upper()
    max_lev = int(selected.get("maxLeverage") or 1)
    leverage = max(1, min(_ask_int("Leverage", min(50, max_lev)), max_lev))
    cycles = _ask_int("Max cycles", 50)
    seconds = _ask_int("Max session seconds", 1800)
    margin = _ask_float("Target margin per IOC cycle, USDT", 2.0)

    console.print(f"Starting LIVE-SIGNAL/DEMO-EXEC {symbol}: live_changes={float(selected.get('liveChangeRate') or 0):.2f}/s live_trades={float(selected.get('liveTradeRate') or 0):.2f}/s")
    cmd = [sys.executable, "-m", "mexc_tick_scalper.demo_live_signal_test", "--symbol", symbol, "--session-seconds", str(seconds), "--max-cycles", str(cycles), "--leverage", str(leverage), "--target-margin-usdt", str(margin), "--auto-flatten-start"]
    completed = subprocess.run(cmd, env=os.environ.copy())
    raise SystemExit(completed.returncode)


def main() -> None:
    try:
        asyncio.run(main_async())
    except (MexcWebError, ValueError, IndexError) as exc:
        console.print(f"[red]LIVE DEMO LAUNCHER FAILED:[/red] {exc}")
        raise SystemExit(2) from exc


if __name__ == "__main__":
    main()
