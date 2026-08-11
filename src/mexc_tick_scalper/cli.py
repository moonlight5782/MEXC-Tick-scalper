from __future__ import annotations

import argparse
import asyncio
import math
import time

from rich.console import Console
from rich.table import Table

from .config import load_config
from .fees import provider_from_config
from .market import MexcPublicMarket
from .scanner import scan_candidates
from .shadow import best_result

console = Console()


def _format_pf(value: float) -> str:
    return "∞" if math.isinf(value) else f"{value:.2f}"


async def cmd_scan(args: argparse.Namespace) -> None:
    cfg = load_config(args.config)
    market_cfg = cfg.get("mexc", {})
    provider = provider_from_config(cfg)
    candidates = await scan_candidates(
        market_cfg.get("rest_base_url", "https://api.mexc.com"), cfg, provider
    )
    if not candidates:
        console.print("[yellow]No tradeable candidates.[/yellow] Only explicitly verified 0% maker + 0% taker symbols are eligible.")
        return

    limit = int(cfg.get("scanner", {}).get("max_symbols_for_shadow", 12))
    table = Table(title="Zero-fee scanner candidates")
    table.add_column("Symbol")
    table.add_column("Last", justify="right")
    table.add_column("Spread bps", justify="right")
    table.add_column("Volume24", justify="right")
    for c in candidates[:limit]:
        table.add_row(c.symbol, f"{c.last:g}", f"{c.spread_bps:.2f}", f"{c.volume24:,.0f}")
    console.print(table)


async def cmd_shadow(args: argparse.Namespace) -> None:
    cfg = load_config(args.config)
    provider = provider_from_config(cfg)
    symbol = args.symbol.upper()
    status = provider.status(symbol)
    if not status.zero_confirmed:
        console.print(f"[red]BLOCKED[/red] {symbol}: zero maker+taker fee is not verified in config.")
        return

    market_cfg = cfg.get("mexc", {})
    market = MexcPublicMarket(
        market_cfg.get("rest_base_url", "https://api.mexc.com"),
        market_cfg.get("websocket_url", "wss://contract.mexc.com/edge"),
    )

    duration = int(args.seconds)
    ticks = []
    deadline = time.monotonic() + duration
    console.print(f"Collecting {symbol} trade ticks for {duration}s...")
    async for tick in market.trades(symbol):
        ticks.append(tick)
        if time.monotonic() >= deadline:
            break

    strategy = cfg.get("strategy", {})
    result = best_result(
        symbol=symbol,
        ticks=ticks,
        momentum_grid=[int(x) for x in strategy.get("momentum_ticks", [2, 3, 4, 5, 6])],
        reversal_grid=[int(x) for x in strategy.get("reversal_ticks", [1, 2])],
        max_hold_seconds=int(strategy.get("max_hold_seconds", 180)),
        min_trades=int(strategy.get("min_shadow_trades", 20)),
    )

    console.print(f"Captured {len(ticks)} ticks")
    if result is None:
        console.print("[yellow]Not enough completed shadow trades to rank parameters.[/yellow]")
        return

    min_pf = float(strategy.get("min_profit_factor", 1.30))
    min_exp = float(strategy.get("min_expectancy_bps", 0.0))
    eligible = result.profit_factor >= min_pf and result.expectancy_bps > min_exp

    table = Table(title=f"Best shadow result: {symbol}")
    table.add_column("Momentum")
    table.add_column("Reversal")
    table.add_column("Trades")
    table.add_column("Win rate")
    table.add_column("PF")
    table.add_column("Expectancy bps")
    table.add_column("Max DD bps")
    win_rate = result.wins / result.trades * 100 if result.trades else 0
    table.add_row(
        str(result.momentum_ticks), str(result.reversal_ticks), str(result.trades),
        f"{win_rate:.1f}%", _format_pf(result.profit_factor),
        f"{result.expectancy_bps:.3f}", f"{result.max_drawdown_bps:.2f}",
    )
    console.print(table)
    console.print("[green]EDGE CANDIDATE[/green]" if eligible else "[yellow]SHADOW ONLY[/yellow]")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="mexc-scalper")
    sub = p.add_subparsers(dest="command", required=True)

    scan = sub.add_parser("scan", help="List liquid symbols that passed the strict zero-fee gate")
    scan.add_argument("--config", default="config.yaml")

    shadow = sub.add_parser("shadow", help="Collect real trade ticks and optimize shadow parameters")
    shadow.add_argument("--symbol", required=True)
    shadow.add_argument("--seconds", type=int, default=120)
    shadow.add_argument("--config", default="config.yaml")
    return p


def main() -> None:
    args = build_parser().parse_args()
    if args.command == "scan":
        asyncio.run(cmd_scan(args))
    elif args.command == "shadow":
        asyncio.run(cmd_shadow(args))


if __name__ == "__main__":
    main()
