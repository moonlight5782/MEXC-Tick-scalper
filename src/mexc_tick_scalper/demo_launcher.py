from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from rich.console import Console
from rich.table import Table

from .demo_discovery import _fetch_contracts
from .execution import OrderSide
from .web_execution import MexcWebError, MexcWebExecutionAdapter, WebExecutionConfig
from .web_fee import provider_from_web_fee_payload

console = Console()


def _load_project_env() -> None:
    project_root = Path(__file__).resolve().parents[2]
    load_dotenv(project_root / ".env", override=False)


async def _tradable_zero_fee_contracts() -> list[dict[str, Any]]:
    cfg = WebExecutionConfig.demo_from_env(write_enabled=False)
    async with MexcWebExecutionAdapter(cfg) as adapter:
        contracts = await _fetch_contracts(adapter)
        fee_payload = await adapter.get_fee_rates()
        provider = provider_from_web_fee_payload(fee_payload)

        candidates: list[dict[str, Any]] = []
        for row in contracts:
            symbol = str(row.get("symbol", "")).upper()
            status = provider.status(symbol)
            if status.maker != 0 or status.taker != 0:
                continue
            try:
                ask = await adapter.get_best_price(symbol, OrderSide.LONG)
                bid = await adapter.get_best_price(symbol, OrderSide.SHORT)
            except MexcWebError:
                continue
            if ask <= 0 or bid <= 0 or ask < bid:
                continue
            enriched = dict(row)
            enriched["bestAsk"] = ask
            enriched["bestBid"] = bid
            enriched["spreadPct"] = ((ask - bid) / ((ask + bid) / 2.0)) * 100 if ask + bid > 0 else 0.0
            candidates.append(enriched)

    candidates.sort(key=lambda row: (float(row.get("spreadPct") or 999.0), str(row.get("symbol", ""))))
    return candidates


def _show(rows: list[dict[str, Any]]) -> None:
    table = Table(title=f"MEXC Demo tradable zero-fee pairs ({len(rows)})")
    table.add_column("#", justify="right")
    table.add_column("Symbol")
    table.add_column("Bid", justify="right")
    table.add_column("Ask", justify="right")
    table.add_column("Spread %", justify="right")
    table.add_column("Max lev", justify="right")
    for idx, row in enumerate(rows, 1):
        table.add_row(
            str(idx),
            str(row.get("symbol", "?")),
            f"{float(row.get('bestBid') or 0):g}",
            f"{float(row.get('bestAsk') or 0):g}",
            f"{float(row.get('spreadPct') or 0):.4f}",
            str(row.get("maxLeverage", "?")),
        )
    console.print(table)


def _ask_int(prompt: str, default: int) -> int:
    raw = input(f"{prompt} [{default}]: ").strip()
    return default if not raw else int(raw)


def _ask_float(prompt: str, default: float) -> float:
    raw = input(f"{prompt} [{default:g}]: ").strip()
    return default if not raw else float(raw)


async def main_async() -> None:
    _load_project_env()
    rows = await _tradable_zero_fee_contracts()
    if not rows:
        raise MexcWebError("no Demo contracts currently have confirmed 0/0 fee and a usable bid/ask book")

    _show(rows)
    choice = input("Select pair number (or A for automatic tightest-spread candidate): ").strip().lower()
    if choice in {"a", "auto", ""}:
        selected = rows[0]
    else:
        idx = int(choice)
        if idx < 1 or idx > len(rows):
            raise MexcWebError("invalid pair number")
        selected = rows[idx - 1]

    symbol = str(selected.get("symbol", "")).upper()
    max_lev = int(selected.get("maxLeverage") or 1)
    leverage = max(1, min(_ask_int("Leverage", min(50, max_lev)), max_lev))
    max_cycles = _ask_int("Max cycles", 10)
    seconds = _ask_int("Max session seconds", 300)

    mode = input("Strategy [H=Reconstructed hybrid, C=Classic ticks] [H]: ").strip().lower()
    hybrid = mode not in {"c", "classic"}
    module = "mexc_tick_scalper.demo_hybrid_test" if hybrid else "mexc_tick_scalper.demo_tick_test"
    label = "RECONSTRUCTED HYBRID" if hybrid else "CLASSIC"

    target_margin = _ask_float("Target margin per IOC cycle, USDT", 2.0) if hybrid else 0.0

    console.print(f"Starting {label} {symbol} at {leverage}x, max_cycles={max_cycles}, session={seconds}s")
    cmd = [
        sys.executable,
        "-m",
        module,
        "--symbol",
        symbol,
        "--session-seconds",
        str(seconds),
        "--max-cycles",
        str(max_cycles),
        "--leverage",
        str(leverage),
    ]
    if hybrid:
        cmd += ["--target-margin-usdt", str(target_margin)]
    else:
        cmd += ["--momentum-ticks", "3", "--reversal-ticks", "1"]

    completed = subprocess.run(cmd, env=os.environ.copy())
    raise SystemExit(completed.returncode)


def main() -> None:
    try:
        asyncio.run(main_async())
    except (MexcWebError, ValueError) as exc:
        console.print(f"[red]DEMO LAUNCHER FAILED:[/red] {exc}")
        raise SystemExit(2) from exc


if __name__ == "__main__":
    main()
