from __future__ import annotations

import asyncio

from rich.console import Console
from rich.table import Table

from .live_zero_fee_universe import discover_live_zero_fee_crosslisted
from .web_execution import MexcWebError

console = Console()


async def run() -> int:
    rows = await discover_live_zero_fee_crosslisted()
    table = Table(title="LIVE account zero-fee MEXC pairs cross-listed on Binance USD-M")
    table.add_column("#", justify="right")
    table.add_column("MEXC")
    table.add_column("Binance")
    table.add_column("Max lev", justify="right")
    table.add_column("Contract size", justify="right")
    table.add_column("Min vol", justify="right")
    for idx, row in enumerate(rows, 1):
        table.add_row(
            str(idx),
            row.mexc_symbol,
            row.binance_symbol,
            str(row.max_leverage),
            f"{row.contract_size:g}",
            f"{row.min_vol:g}",
        )
    console.print(table)
    console.print(
        f"Found {len(rows)} LIVE cross-listed pair(s) with exact maker=0 and taker=0. "
        "This command is read-only: LIVE execution writes are disabled."
    )
    return len(rows)


def main() -> None:
    try:
        asyncio.run(run())
    except MexcWebError as exc:
        console.print(f"[red]LIVE ZERO-FEE SCAN FAILED:[/red] {exc}")
        raise SystemExit(2) from exc


if __name__ == "__main__":
    main()
