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
from .web_execution import MexcWebError, MexcWebExecutionAdapter, WebExecutionConfig
from .web_fee import provider_from_web_fee_payload

console = Console()


def _load_project_env() -> None:
    """Load the repository .env regardless of how the launcher was started."""
    project_root = Path(__file__).resolve().parents[2]
    env_path = project_root / ".env"
    load_dotenv(env_path, override=False)


async def _zero_fee_contracts() -> list[dict[str, Any]]:
    cfg = WebExecutionConfig.demo_from_env(write_enabled=False)
    async with MexcWebExecutionAdapter(cfg) as adapter:
        contracts = await _fetch_contracts(adapter)
        fee_payload = await adapter.get_fee_rates()
    provider = provider_from_web_fee_payload(fee_payload)
    result: list[dict[str, Any]] = []
    for row in contracts:
        symbol = str(row.get("symbol", "")).upper()
        status = provider.status(symbol)
        if status.maker == 0 and status.taker == 0:
            result.append(row)
    return result


def _show(rows: list[dict[str, Any]]) -> None:
    table = Table(title=f"MEXC Demo zero-fee pairs ({len(rows)})")
    table.add_column("#", justify="right")
    table.add_column("Symbol")
    table.add_column("Max lev", justify="right")
    table.add_column("Contract size", justify="right")
    table.add_column("Min vol", justify="right")
    for idx, row in enumerate(rows, 1):
        table.add_row(
            str(idx),
            str(row.get("symbol", "?")),
            str(row.get("maxLeverage", "?")),
            str(row.get("contractSize", "?")),
            str(row.get("minVol", "?")),
        )
    console.print(table)


def _ask_int(prompt: str, default: int) -> int:
    raw = input(f"{prompt} [{default}]: ").strip()
    if not raw:
        return default
    return int(raw)


async def main_async() -> None:
    _load_project_env()
    rows = await _zero_fee_contracts()
    if not rows:
        raise MexcWebError("no Demo contracts with confirmed maker=0 and taker=0")

    _show(rows)
    choice = input("Select pair number (or A for automatic first candidate): ").strip().lower()
    if choice in {"a", "auto", ""}:
        selected = rows[0]
    else:
        idx = int(choice)
        if idx < 1 or idx > len(rows):
            raise MexcWebError("invalid pair number")
        selected = rows[idx - 1]

    symbol = str(selected.get("symbol", "")).upper()
    max_lev = int(selected.get("maxLeverage") or 1)
    leverage = _ask_int("Leverage", min(50, max_lev))
    leverage = max(1, min(leverage, max_lev))
    max_cycles = _ask_int("Max cycles", 10)
    seconds = _ask_int("Max session seconds", 300)

    console.print(f"Starting {symbol} at {leverage}x, max_cycles={max_cycles}, session={seconds}s")
    cmd = [
        sys.executable,
        "-m",
        "mexc_tick_scalper.demo_tick_test",
        "--symbol",
        symbol,
        "--session-seconds",
        str(seconds),
        "--max-cycles",
        str(max_cycles),
        "--momentum-ticks",
        "3",
        "--reversal-ticks",
        "1",
        "--leverage",
        str(leverage),
    ]
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
