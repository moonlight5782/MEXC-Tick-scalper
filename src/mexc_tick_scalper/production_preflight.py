from __future__ import annotations

import asyncio
from dataclasses import dataclass

from rich.console import Console
from rich.table import Table

from .demo_discovery import _fetch_contracts
from .live_zero_fee_universe import discover_live_zero_fee_crosslisted
from .web_execution import MexcWebExecutionAdapter, MexcWebError, WebExecutionConfig

console = Console()


@dataclass(frozen=True, slots=True)
class ProductionPreflight:
    live_read_only: bool
    live_account_reachable: bool
    live_open_positions: int
    live_zero_fee_pairs: int
    demo_contracts: int
    demo_intersection: int
    private_live_writes_enabled: bool

    @property
    def infrastructure_ready(self) -> bool:
        return (
            self.live_read_only
            and self.live_account_reachable
            and self.live_zero_fee_pairs > 0
            and self.demo_contracts > 0
            and not self.private_live_writes_enabled
        )


async def collect_preflight() -> ProductionPreflight:
    # LIVE is deliberately read-only. Production readiness must never silently
    # mutate this boundary.
    live_cfg = WebExecutionConfig.from_env(write_enabled=False)
    live_cfg.validate_environment()
    live_adapter = MexcWebExecutionAdapter(live_cfg)
    try:
        live_probe = await live_adapter.probe()
        positions = await live_adapter.get_positions()
    finally:
        await live_adapter.close()

    live_zero = await discover_live_zero_fee_crosslisted()

    demo_contract_count = 0
    demo_symbols: set[str] = set()
    try:
        demo_cfg = WebExecutionConfig.demo_from_env(write_enabled=False)
        async with MexcWebExecutionAdapter(demo_cfg) as demo_adapter:
            rows = await _fetch_contracts(demo_adapter)
            demo_symbols = {str(row.get("symbol", "")).upper() for row in rows if row.get("symbol")}
            demo_contract_count = len(demo_symbols)
    except MexcWebError:
        # Demo is useful for execution validation, but lack of a current Demo
        # token must not be confused with LIVE account failure.
        demo_symbols = set()
        demo_contract_count = 0

    live_zero_symbols = {row.mexc_symbol.upper() for row in live_zero}
    return ProductionPreflight(
        live_read_only=True,
        live_account_reachable=bool(live_probe),
        live_open_positions=len(positions),
        live_zero_fee_pairs=len(live_zero_symbols),
        demo_contracts=demo_contract_count,
        demo_intersection=len(live_zero_symbols & demo_symbols),
        private_live_writes_enabled=bool(live_cfg.write_enabled),
    )


def render(result: ProductionPreflight) -> None:
    table = Table(title="MEXC Tick Scalper production preflight")
    table.add_column("Check")
    table.add_column("Value", justify="right")
    table.add_row("LIVE adapter", "READ-ONLY" if result.live_read_only else "UNSAFE")
    table.add_row("LIVE account reachable", "YES" if result.live_account_reachable else "NO")
    table.add_row("LIVE open positions", str(result.live_open_positions))
    table.add_row("LIVE exact 0/0 + Binance pairs", str(result.live_zero_fee_pairs))
    table.add_row("Demo contracts", str(result.demo_contracts))
    table.add_row("LIVE 0/0 ∩ Demo", str(result.demo_intersection))
    table.add_row("PRIVATE LIVE writes", "ENABLED" if result.private_live_writes_enabled else "DISABLED")
    console.print(table)
    if result.infrastructure_ready:
        console.print("[green]INFRASTRUCTURE PREFLIGHT PASS[/green]: LIVE data/account reads are available and private LIVE writes remain disabled.")
    else:
        console.print("[red]INFRASTRUCTURE PREFLIGHT FAIL[/red]: fix the failed checks before any production release work.")

    console.print(
        "[yellow]CAPITAL RELEASE STATUS: NOT CLEARED[/yellow]. "
        "The repository has mixed 100-trade validation results and the official MEXC Futures API currently uses a separate non-zero fee schedule. "
        "This command is intentionally read-only and does not enable LIVE order placement."
    )


async def _main() -> int:
    try:
        result = await collect_preflight()
    except Exception as exc:
        console.print(f"[red]PREFLIGHT ERROR[/red]: {type(exc).__name__}: {exc}")
        return 2
    render(result)
    return 0 if result.infrastructure_ready else 1


def main() -> None:
    raise SystemExit(asyncio.run(_main()))


if __name__ == "__main__":
    main()
