from __future__ import annotations

import argparse
import asyncio
import math
import time
import uuid

from rich.console import Console
from rich.table import Table

from .backtest import walk_forward
from .config import load_config
from .execution import OrderSide
from .fees import provider_from_config
from .market import MexcPublicMarket
from .scanner import scan_candidates
from .shadow import best_result
from .tick_data import append_tick_csv, load_ticks_csv
from .web_execution import MexcWebError, MexcWebExecutionAdapter, WebExecutionConfig

console = Console()


def _format_pf(value: float) -> str:
    return "∞" if math.isinf(value) else f"{value:.2f}"


def _add_result_row(table: Table, result) -> None:
    win_rate = result.wins / result.trades * 100 if result.trades else 0
    table.add_row(
        str(result.momentum_ticks),
        str(result.reversal_ticks),
        str(result.trades),
        f"{win_rate:.1f}%",
        _format_pf(result.profit_factor),
        f"{result.expectancy_bps:.3f}",
        f"{result.max_drawdown_bps:.2f}",
    )


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


async def _collect_ticks(args: argparse.Namespace, *, write_path: str | None = None):
    cfg = load_config(args.config)
    symbol = args.symbol.upper()
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
        if write_path:
            append_tick_csv(write_path, tick)
        if time.monotonic() >= deadline:
            break
    return cfg, symbol, ticks


async def cmd_shadow(args: argparse.Namespace) -> None:
    cfg = load_config(args.config)
    provider = provider_from_config(cfg)
    symbol = args.symbol.upper()
    status = provider.status(symbol)
    if not status.zero_confirmed:
        console.print(f"[red]BLOCKED[/red] {symbol}: zero maker+taker fee is not verified in config.")
        return

    cfg, symbol, ticks = await _collect_ticks(args)
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
    for name in ("Momentum", "Reversal", "Trades", "Win rate", "PF", "Expectancy bps", "Max DD bps"):
        table.add_column(name)
    _add_result_row(table, result)
    console.print(table)
    console.print("[green]EDGE CANDIDATE[/green]" if eligible else "[yellow]SHADOW ONLY[/yellow]")


async def cmd_record(args: argparse.Namespace) -> None:
    _, symbol, ticks = await _collect_ticks(args, write_path=args.output)
    console.print(f"[green]Saved[/green] {len(ticks)} {symbol} ticks to {args.output}")


def cmd_backtest(args: argparse.Namespace) -> None:
    cfg = load_config(args.config)
    symbol = args.symbol.upper()
    ticks = load_ticks_csv(args.input, symbol=symbol)
    strategy = cfg.get("strategy", {})
    result = walk_forward(
        symbol=symbol,
        ticks=ticks,
        momentum_grid=[int(x) for x in strategy.get("momentum_ticks", [2, 3, 4, 5, 6])],
        reversal_grid=[int(x) for x in strategy.get("reversal_ticks", [1, 2])],
        max_hold_seconds=int(strategy.get("max_hold_seconds", 180)),
        min_train_trades=int(strategy.get("min_shadow_trades", 20)),
        train_fraction=float(args.train_fraction),
    )
    if result is None:
        console.print("[yellow]Not enough data/trades for walk-forward backtest.[/yellow]")
        return

    table = Table(title=f"Walk-forward backtest: {symbol}")
    table.add_column("Set")
    for name in ("Momentum", "Reversal", "Trades", "Win rate", "PF", "Expectancy bps", "Max DD bps"):
        table.add_column(name)
    for label, r in (("TRAIN", result.train), ("VALIDATION", result.validation)):
        win_rate = r.wins / r.trades * 100 if r.trades else 0
        table.add_row(
            label,
            str(r.momentum_ticks), str(r.reversal_ticks), str(r.trades),
            f"{win_rate:.1f}%", _format_pf(r.profit_factor),
            f"{r.expectancy_bps:.3f}", f"{r.max_drawdown_bps:.2f}",
        )
    console.print(table)

    min_pf = float(strategy.get("min_profit_factor", 1.30))
    min_exp = float(strategy.get("min_expectancy_bps", 0.0))
    passed = (
        result.validation.trades > 0
        and result.validation.profit_factor >= min_pf
        and result.validation.expectancy_bps > min_exp
    )
    console.print("[green]VALIDATION PASSED[/green]" if passed else "[red]VALIDATION FAILED[/red]")


async def cmd_web_probe(args: argparse.Namespace) -> None:
    """Read-only probe of a WEB session. It cannot place or close orders."""
    try:
        web_cfg = WebExecutionConfig.from_env(base_url=args.base_url, write_enabled=False)
        async with MexcWebExecutionAdapter(web_cfg) as adapter:
            result = await adapter.probe()
            fees = None
            try:
                fees = await adapter.get_fee_rates()
            except MexcWebError as exc:
                console.print(f"[yellow]Fee endpoint unavailable:[/yellow] {exc}")

        console.print(f"[green]WEB session authenticated[/green] against {web_cfg.base_url}")
        asset_data = result.get("asset", {}).get("data") if isinstance(result.get("asset"), dict) else None
        if isinstance(asset_data, dict):
            available = asset_data.get("availableBalance", asset_data.get("available", "?"))
            equity = asset_data.get("equity", asset_data.get("cashBalance", "?"))
            console.print(f"USDT available={available} equity={equity}")
        positions = result.get("positions", {}).get("data", []) if isinstance(result.get("positions"), dict) else []
        console.print(f"Open positions: {len(positions or [])}")
        if fees is not None:
            fee_data = fees.get("data") if isinstance(fees, dict) else None
            count = len(fee_data) if isinstance(fee_data, list) else (1 if fee_data else 0)
            console.print(f"Fee records readable: {count}")
    except MexcWebError as exc:
        console.print(f"[red]WEB probe failed:[/red] {exc}")
        raise SystemExit(2) from exc


async def cmd_demo_check(args: argparse.Namespace) -> None:
    """Read-only MEXC Demo Trading session check."""
    try:
        demo_cfg = WebExecutionConfig.demo_from_env(write_enabled=False)
        async with MexcWebExecutionAdapter(demo_cfg) as adapter:
            result = await adapter.probe()
            detail = await adapter.get_contract_detail(args.symbol.upper()) if args.symbol else None
        console.print(f"[green]DEMO session authenticated[/green] against {demo_cfg.base_url}")
        asset_data = result.get("asset", {}).get("data") if isinstance(result.get("asset"), dict) else None
        if isinstance(asset_data, dict):
            available = asset_data.get("availableBalance", asset_data.get("available", "?"))
            equity = asset_data.get("equity", asset_data.get("cashBalance", "?"))
            console.print(f"Demo USDT available={available} equity={equity}")
        positions = result.get("positions", {}).get("data", []) if isinstance(result.get("positions"), dict) else []
        console.print(f"Demo open positions: {len(positions or [])}")
        if detail:
            console.print(
                f"{args.symbol.upper()}: contractSize={detail.get('contractSize')} "
                f"minVol={detail.get('minVol')} maxLeverage={detail.get('maxLeverage')}"
            )
    except MexcWebError as exc:
        console.print(f"[red]DEMO check failed:[/red] {exc}")
        raise SystemExit(2) from exc


async def cmd_demo_roundtrip(args: argparse.Namespace) -> None:
    """Place one small IOC order and immediately flatten it on MEXC Demo only."""
    if not args.confirm_demo_order:
        console.print("[red]Refusing demo write.[/red] Add --confirm-demo-order to place simulated orders.")
        raise SystemExit(2)
    if args.notional_usdt <= 0 or args.leverage <= 0:
        raise SystemExit("--notional-usdt and --leverage must be positive")

    symbol = args.symbol.upper()
    side = OrderSide.LONG if args.side.lower() == "long" else OrderSide.SHORT
    close_action_side = OrderSide.SHORT if side is OrderSide.LONG else OrderSide.LONG
    try:
        demo_cfg = WebExecutionConfig.demo_from_env(write_enabled=True)
        async with MexcWebExecutionAdapter(demo_cfg) as adapter:
            existing = await adapter.get_position(symbol)
            if existing is not None:
                raise MexcWebError(f"refusing roundtrip: demo position already open for {symbol}")
            price = await adapter.get_best_price(symbol, side)
            qty = args.notional_usdt / price
            entry_id = f"demo-e-{uuid.uuid4().hex[:20]}"
            entry = await adapter.open_ioc(
                symbol=symbol,
                side=side,
                price=price,
                qty=qty,
                leverage=args.leverage,
                client_order_id=entry_id,
            )
            console.print(
                f"DEMO IOC requested={entry.requested_qty:g} filled={entry.filled_qty:g} "
                f"avg={entry.avg_price:g} fee={entry.fee_usdt:g}"
            )
            if entry.filled_qty <= 0:
                console.print("[yellow]IOC did not fill; nothing to close.[/yellow]")
                return
            exit_id = f"demo-x-{uuid.uuid4().hex[:20]}"
            exit_fill = await adapter.close_market_reduce_only(
                symbol=symbol,
                qty=entry.filled_qty,
                side=close_action_side,
                client_order_id=exit_id,
            )
            remaining = await adapter.get_position(symbol)
            console.print(
                f"DEMO EXIT filled={exit_fill.filled_qty:g} avg={exit_fill.avg_price:g} "
                f"fee={exit_fill.fee_usdt:g}"
            )
            if remaining is None:
                console.print("[green]DEMO ROUNDTRIP PASSED: position is flat.[/green]")
            else:
                console.print(f"[red]DEMO ROUNDTRIP WARNING: remaining qty={remaining.qty:g}[/red]")
                raise SystemExit(3)
    except MexcWebError as exc:
        console.print(f"[red]DEMO roundtrip failed:[/red] {exc}")
        raise SystemExit(2) from exc


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="mexc-scalper")
    sub = p.add_subparsers(dest="command", required=True)

    scan = sub.add_parser("scan", help="List liquid symbols that passed the strict zero-fee gate")
    scan.add_argument("--config", default="config.yaml")

    shadow = sub.add_parser("shadow", help="Collect real trade ticks and optimize shadow parameters")
    shadow.add_argument("--symbol", required=True)
    shadow.add_argument("--seconds", type=int, default=120)
    shadow.add_argument("--config", default="config.yaml")

    record = sub.add_parser("record", help="Record real trade ticks to CSV for replay/backtests")
    record.add_argument("--symbol", required=True)
    record.add_argument("--seconds", type=int, default=600)
    record.add_argument("--output", required=True)
    record.add_argument("--config", default="config.yaml")

    backtest = sub.add_parser("backtest", help="Run walk-forward replay on a recorded tick CSV")
    backtest.add_argument("--symbol", required=True)
    backtest.add_argument("--input", required=True)
    backtest.add_argument("--train-fraction", type=float, default=0.70)
    backtest.add_argument("--config", default="config.yaml")

    web_probe = sub.add_parser("web-probe", help="Read-only WEB-session auth/balance/position probe")
    web_probe.add_argument("--base-url", default=None, help="Override MEXC_WEB_BASE_URL")

    demo_check = sub.add_parser("demo-check", help="Read-only MEXC Demo Trading session/contract check")
    demo_check.add_argument("--symbol", default="BTC_USDT")

    demo_roundtrip = sub.add_parser("demo-roundtrip", help="One tiny IOC entry + immediate market flatten on Demo")
    demo_roundtrip.add_argument("--symbol", default="BTC_USDT")
    demo_roundtrip.add_argument("--side", choices=["long", "short"], default="long")
    demo_roundtrip.add_argument("--notional-usdt", type=float, default=10.0)
    demo_roundtrip.add_argument("--leverage", type=int, default=5)
    demo_roundtrip.add_argument("--confirm-demo-order", action="store_true")
    return p


def main() -> None:
    args = build_parser().parse_args()
    if args.command == "scan":
        asyncio.run(cmd_scan(args))
    elif args.command == "shadow":
        asyncio.run(cmd_shadow(args))
    elif args.command == "record":
        asyncio.run(cmd_record(args))
    elif args.command == "backtest":
        cmd_backtest(args)
    elif args.command == "web-probe":
        asyncio.run(cmd_web_probe(args))
    elif args.command == "demo-check":
        asyncio.run(cmd_demo_check(args))
    elif args.command == "demo-roundtrip":
        asyncio.run(cmd_demo_roundtrip(args))


if __name__ == "__main__":
    main()
