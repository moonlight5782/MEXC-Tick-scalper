from __future__ import annotations

import argparse
import asyncio

from rich.console import Console
from rich.table import Table

from .config import load_config
from .scanner import scan_candidates
from .web_execution import MexcWebError, MexcWebExecutionAdapter, WebExecutionConfig
from .web_fee import read_web_fee_provider

console = Console()


async def run(args: argparse.Namespace) -> None:
    cfg = load_config(args.config)
    market_cfg = cfg.get("mexc", {})
    web_cfg = WebExecutionConfig.from_env(write_enabled=False)

    async with MexcWebExecutionAdapter(web_cfg) as adapter:
        provider = await read_web_fee_provider(adapter)

    zero_fee_count = sum(1 for status in provider.fees.values() if status.zero_confirmed)
    if zero_fee_count == 0:
        console.print("[red]No account-specific maker=0 AND taker=0 symbols were confirmed.[/red]")
        return

    candidates = await scan_candidates(
        market_cfg.get("rest_base_url", "https://api.mexc.com"),
        cfg,
        provider,
    )
    console.print(f"Account zero-fee symbols: {zero_fee_count}; liquid scanner candidates: {len(candidates)}")
    if not candidates:
        return

    limit = int(args.limit)
    table = Table(title="Account-specific 0% fee candidates")
    table.add_column("Symbol")
    table.add_column("Last", justify="right")
    table.add_column("Spread bps", justify="right")
    table.add_column("Volume24", justify="right")
    for candidate in candidates[:limit]:
        table.add_row(
            candidate.symbol,
            f"{candidate.last:g}",
            f"{candidate.spread_bps:.2f}",
            f"{candidate.volume24:,.0f}",
        )
    console.print(table)


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only account-specific MEXC zero-fee scanner")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--limit", type=int, default=20)
    args = parser.parse_args()
    try:
        asyncio.run(run(args))
    except MexcWebError as exc:
        console.print(f"[red]WEB scanner failed:[/red] {exc}")
        raise SystemExit(2) from exc


if __name__ == "__main__":
    main()
