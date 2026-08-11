from __future__ import annotations

import argparse
import asyncio
from typing import Any

from rich.console import Console
from rich.table import Table

from .web_execution import MexcWebError, MexcWebExecutionAdapter, WebExecutionConfig
from .web_fee import provider_from_web_fee_payload

console = Console()


def _contract_rows(payload: Any) -> list[dict[str, Any]]:
    data = payload.get("data") if isinstance(payload, dict) else payload
    if isinstance(data, list):
        return [row for row in data if isinstance(row, dict)]
    if isinstance(data, dict):
        return [data]
    return []


async def _fetch_contracts(adapter: MexcWebExecutionAdapter) -> list[dict[str, Any]]:
    # MEXC /contract/detail without a symbol returns the contracts available in
    # the current environment. We deliberately use the Demo adapter here so the
    # hard host guard still applies.
    payload = await adapter._request("GET", "/contract/detail")
    rows = _contract_rows(payload)
    rows = [row for row in rows if row.get("symbol")]
    rows.sort(key=lambda row: str(row.get("symbol", "")))
    return rows


def _contract_table(rows: list[dict[str, Any]], *, title: str) -> Table:
    table = Table(title=title)
    table.add_column("Symbol")
    table.add_column("Contract size", justify="right")
    table.add_column("Min vol", justify="right")
    table.add_column("Max vol", justify="right")
    table.add_column("Max lev", justify="right")
    table.add_column("Price scale", justify="right")
    for row in rows:
        table.add_row(
            str(row.get("symbol", "?")),
            str(row.get("contractSize", "?")),
            str(row.get("minVol", "?")),
            str(row.get("maxVol", "?")),
            str(row.get("maxLeverage", "?")),
            str(row.get("priceScale", "?")),
        )
    return table


async def cmd_contracts(args: argparse.Namespace) -> None:
    cfg = WebExecutionConfig.demo_from_env(write_enabled=False)
    async with MexcWebExecutionAdapter(cfg) as adapter:
        rows = await _fetch_contracts(adapter)
    if args.limit and args.limit > 0:
        shown = rows[: args.limit]
    else:
        shown = rows
    console.print(_contract_table(shown, title=f"MEXC Demo contracts ({len(rows)} total)"))


async def cmd_scan(args: argparse.Namespace) -> None:
    cfg = WebExecutionConfig.demo_from_env(write_enabled=False)
    async with MexcWebExecutionAdapter(cfg) as adapter:
        contracts = await _fetch_contracts(adapter)
        fee_payload = await adapter.get_fee_rates()

    provider = provider_from_web_fee_payload(fee_payload)
    zero_fee: list[tuple[dict[str, Any], float, float]] = []
    unknown = 0
    nonzero = 0

    for row in contracts:
        symbol = str(row.get("symbol", "")).upper()
        status = provider.status(symbol)
        if status.maker is None or status.taker is None:
            unknown += 1
            continue
        if status.maker == 0 and status.taker == 0:
            zero_fee.append((row, status.maker, status.taker))
        else:
            nonzero += 1

    table = Table(title=f"Demo zero-fee contracts ({len(zero_fee)} found)")
    table.add_column("Symbol")
    table.add_column("Maker", justify="right")
    table.add_column("Taker", justify="right")
    table.add_column("Max lev", justify="right")
    table.add_column("Contract size", justify="right")
    table.add_column("Min vol", justify="right")
    for row, maker, taker in zero_fee:
        table.add_row(
            str(row.get("symbol", "?")),
            f"{maker:g}",
            f"{taker:g}",
            str(row.get("maxLeverage", "?")),
            str(row.get("contractSize", "?")),
            str(row.get("minVol", "?")),
        )
    console.print(table)
    console.print(
        f"Contracts={len(contracts)}  zero_fee={len(zero_fee)}  "
        f"nonzero_fee={nonzero}  fee_unknown={unknown}"
    )
    if not zero_fee:
        console.print("[yellow]No Demo contracts have confirmed 0 maker + 0 taker fee for this session.[/yellow]")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Discover MEXC Demo contracts and account-specific zero-fee symbols")
    sub = parser.add_subparsers(dest="command", required=True)

    contracts = sub.add_parser("contracts", help="List every contract available in MEXC Demo")
    contracts.add_argument("--limit", type=int, default=0, help="Show only first N rows; 0 means all")

    sub.add_parser("scan", help="List Demo contracts with confirmed maker=0 and taker=0 for this account")
    return parser


async def _main_async(args: argparse.Namespace) -> None:
    if args.command == "contracts":
        await cmd_contracts(args)
    elif args.command == "scan":
        await cmd_scan(args)


def main() -> None:
    args = build_parser().parse_args()
    try:
        asyncio.run(_main_async(args))
    except MexcWebError as exc:
        console.print(f"[red]DEMO DISCOVERY FAILED:[/red] {exc}")
        raise SystemExit(2) from exc


if __name__ == "__main__":
    main()
