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

from .demo_activity import sample_many
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

    if not candidates:
        return []

    console.print(f"Measuring TESTNET tick activity for {len(candidates)} zero-fee pairs...")
    activity = await sample_many([str(row.get("symbol", "")).upper() for row in candidates], seconds=6.0)
    for row in candidates:
        sample = activity.get(str(row.get("symbol", "")).upper())
        row["tickRate"] = sample.trade_rate if sample else 0.0
        row["changeRate"] = sample.change_rate if sample else 0.0
        row["sampleTicks"] = sample.ticks if sample else 0
        row["sampleChanges"] = sample.price_changes if sample else 0

    # For tick scalping, actual price-change activity is primary; trade rate second; spread third.
    candidates.sort(
        key=lambda row: (
            -float(row.get("changeRate") or 0.0),
            -float(row.get("tickRate") or 0.0),
            float(row.get("spreadPct") or 999.0),
            str(row.get("symbol", "")),
        )
    )
    return candidates


def _show(rows: list[dict[str, Any]]) -> None:
    table = Table(title=f"MEXC Demo zero-fee pairs ranked by TESTNET activity ({len(rows)})")
    table.add_column("#", justify="right")
    table.add_column("Symbol")
    table.add_column("Price chg/s", justify="right")
    table.add_column("Trades/s", justify="right")
    table.add_column("Spread %", justify="right")
    table.add_column("Max lev", justify="right")
    for idx, row in enumerate(rows, 1):
        table.add_row(
            str(idx),
            str(row.get("symbol", "?")),
            f"{float(row.get('changeRate') or 0):.2f}",
            f"{float(row.get('tickRate') or 0):.2f}",
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
    active_rows = [row for row in rows if float(row.get("changeRate") or 0) > 0]
    if not active_rows:
        raise MexcWebError("all current zero-fee Demo pairs had 0 price changes during the activity sample; testnet is too inactive for a meaningful tick-scalper test right now")

    choice = input("Select pair number (or A for automatic most-active candidate): ").strip().lower()
    if choice in {"a", "auto", ""}:
        selected = active_rows[0]
    else:
        idx = int(choice)
        if idx < 1 or idx > len(rows):
            raise MexcWebError("invalid pair number")
        selected = rows[idx - 1]
        if float(selected.get("changeRate") or 0) <= 0:
            raise MexcWebError(f"{selected.get('symbol')} had no price changes in the TESTNET activity sample; choose an active pair")

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

    console.print(
        f"Starting {label} {symbol} at {leverage}x, max_cycles={max_cycles}, session={seconds}s "
        f"sample_activity={float(selected.get('changeRate') or 0):.2f} price_changes/s"
    )
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
