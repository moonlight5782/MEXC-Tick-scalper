from __future__ import annotations

import asyncio
import math
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from dotenv import load_dotenv
from rich.console import Console
from rich.table import Table

from .demo_discovery import _fetch_contracts
from .demo_position_manager import flatten_all_demo_positions
from .execution import OrderSide
from .lead_lag import mexc_to_binance_symbol
from .live_lead_lag_scan import run as run_live_lead_lag_scan
from .web_execution import MexcWebError, MexcWebExecutionAdapter, WebExecutionConfig

console = Console()
QUICK_SCAN_WARMUP_SECONDS = 5.0
QUICK_SCAN_SECONDS = 15.0
MIN_LIVE_NET_EDGE_BPS = 2.0


def _load_env() -> None:
    root = Path(__file__).resolve().parents[2]
    load_dotenv(root / ".env", override=False)


def _candidate_score(events: int, avg_edge_bps: float, live_spread_bps: float) -> float:
    net = float(avg_edge_bps) - float(live_spread_bps)
    return net * math.sqrt(max(1, int(events)))


async def _candidates() -> list[dict[str, Any]]:
    console.print(
        f"Refreshing REAL Binance->MEXC lag across the LIVE account 0/0-fee universe "
        f"(~{int(QUICK_SCAN_WARMUP_SECONDS + QUICK_SCAN_SECONDS)}s)..."
    )
    scan_args = SimpleNamespace(
        scan_seconds=QUICK_SCAN_SECONDS,
        warmup_seconds=QUICK_SCAN_WARMUP_SECONDS,
        horizon_ms=250,
        baseline_seconds=20.0,
        min_edge_bps=4.0,
        min_binance_move_bps=1.0,
        max_age_ms=500.0,
        event_cooldown_ms=300,
        mexc_shard_size=20,
        loop_seconds=0.05,
        spread_top=50,
        top=20,
    )
    scan_rows = await run_live_lead_lag_scan(scan_args)
    scan_by_symbol = {row.symbol: row for row in scan_rows}

    demo_cfg = WebExecutionConfig.demo_from_env(write_enabled=False)
    async with MexcWebExecutionAdapter(demo_cfg) as adapter:
        demo_contracts = await _fetch_contracts(adapter)
        ranked: list[dict[str, Any]] = []
        for contract in demo_contracts:
            symbol = str(contract.get("symbol") or "").upper()
            stat = scan_by_symbol.get(symbol)
            if stat is None or stat.events <= 0 or stat.live_spread_bps is None:
                continue
            net_edge = stat.avg_edge_bps - stat.live_spread_bps
            if net_edge < MIN_LIVE_NET_EDGE_BPS:
                continue

            try:
                demo_ask, demo_bid = await asyncio.gather(
                    adapter.get_best_price(symbol, OrderSide.LONG),
                    adapter.get_best_price(symbol, OrderSide.SHORT),
                )
            except MexcWebError:
                continue
            if demo_ask <= 0 or demo_bid <= 0 or demo_ask < demo_bid:
                continue
            demo_mid = (demo_ask + demo_bid) / 2.0
            demo_spread_bps = ((demo_ask - demo_bid) / demo_mid) * 10_000.0 if demo_mid > 0 else math.inf

            item = dict(contract)
            item["events"] = int(stat.events)
            item["longEvents"] = int(stat.long_events)
            item["shortEvents"] = int(stat.short_events)
            item["avgEdgeBps"] = float(stat.avg_edge_bps)
            item["maxEdgeBps"] = float(stat.max_edge_bps)
            item["maxBinanceMoveBps"] = float(stat.max_binance_move_bps)
            item["liveSpreadBps"] = float(stat.live_spread_bps)
            item["liveNetEdgeBps"] = float(net_edge)
            item["demoSpreadBps"] = float(demo_spread_bps)
            item["score"] = _candidate_score(stat.events, stat.avg_edge_bps, stat.live_spread_bps)
            ranked.append(item)

    ranked.sort(
        key=lambda row: (
            -float(row.get("score") or 0),
            -int(row.get("events") or 0),
            -float(row.get("liveNetEdgeBps") or 0),
            float(row.get("liveSpreadBps") or math.inf),
        )
    )
    return ranked


def _show(rows: list[dict[str, Any]]) -> None:
    table = Table(title="Current LIVE 0/0 Binance->MEXC lag opportunities that can execute on Demo")
    table.add_column("#", justify="right")
    table.add_column("MEXC")
    table.add_column("Binance")
    table.add_column("Events", justify="right")
    table.add_column("L/S", justify="right")
    table.add_column("Avg edge", justify="right")
    table.add_column("LIVE spread", justify="right")
    table.add_column("Net edge", justify="right")
    table.add_column("Demo spread", justify="right")
    table.add_column("Max lev", justify="right")
    for idx, row in enumerate(rows[:20], 1):
        symbol = str(row.get("symbol") or "?")
        table.add_row(
            str(idx),
            symbol,
            mexc_to_binance_symbol(symbol),
            str(int(row.get("events") or 0)),
            f"{int(row.get('longEvents') or 0)}/{int(row.get('shortEvents') or 0)}",
            f"{float(row.get('avgEdgeBps') or 0):.3f}bps",
            f"{float(row.get('liveSpreadBps') or 0):.3f}bps",
            f"{float(row.get('liveNetEdgeBps') or 0):+.3f}bps",
            f"{float(row.get('demoSpreadBps') or 0):.3f}bps",
            str(int(row.get("maxLeverage") or 1)),
        )
    console.print(table)
    console.print(
        "Selection economics are LIVE-only: Binance leader + MEXC LIVE lag + REAL account maker/taker=0/0. "
        "The Testnet order book is used only to execute the safe Demo position."
    )


def _ask_int(prompt: str, default: int) -> int:
    raw = input(f"{prompt} [{default}]: ").strip()
    return default if not raw else int(raw)


def _ask_float(prompt: str, default: float) -> float:
    raw = input(f"{prompt} [{default:g}]: ").strip()
    return default if not raw else float(raw)


async def _prepare_session() -> tuple[str, int, int, int, float]:
    await flatten_all_demo_positions(reason="startup")

    rows = await _candidates()
    if not rows:
        raise MexcWebError(
            "no CURRENT LIVE 0/0-fee Binance->MEXC lag candidate with at least "
            f"{MIN_LIVE_NET_EDGE_BPS:g}bps edge after LIVE spread is also available on MEXC Demo. "
            "No trade is safer than forcing a weak setup; rerun when a real lag appears."
        )
    _show(rows)

    choice = input("Select pair number (or A for automatic best current LIVE lag): ").strip().lower()
    selected = rows[0] if choice in {"", "a", "auto"} else rows[int(choice) - 1]

    symbol = str(selected.get("symbol") or "").upper()
    max_lev = int(selected.get("maxLeverage") or 1)
    leverage = max(1, min(_ask_int("Leverage", min(50, max_lev)), max_lev))
    cycles = _ask_int("Max cycles", 50)
    seconds = _ask_int("Max session seconds", 1800)
    margin = _ask_float("Target margin per IOC cycle, USDT", 2.0)

    console.print(
        f"Starting LIVE-LAG -> DEMO {symbol}: Binance={mexc_to_binance_symbol(symbol)} "
        f"events={int(selected.get('events') or 0)} "
        f"avg_edge={float(selected.get('avgEdgeBps') or 0):.2f}bps "
        f"LIVE_spread={float(selected.get('liveSpreadBps') or 0):.2f}bps "
        f"net={float(selected.get('liveNetEdgeBps') or 0):+.2f}bps"
    )
    return symbol, leverage, cycles, seconds, margin


def _cleanup_sync(reason: str) -> bool:
    try:
        asyncio.run(flatten_all_demo_positions(reason=reason))
        return True
    except Exception as exc:
        console.print(f"[red]DEMO CLEANUP FAILED[/red] ({reason}): {exc}")
        return False


def main() -> None:
    _load_env()
    child: subprocess.Popen | None = None
    exit_code = 0

    try:
        symbol, leverage, cycles, seconds, margin = asyncio.run(_prepare_session())
        cmd = [
            sys.executable,
            "-m",
            "mexc_tick_scalper.demo_lead_lag_test",
            "--symbol",
            symbol,
            "--session-seconds",
            str(seconds),
            "--max-cycles",
            str(cycles),
            "--leverage",
            str(leverage),
            "--target-margin-usdt",
            str(margin),
        ]
        child_env = os.environ.copy()
        child_env["MEXC_DEMO_AUTO_FLATTEN_START"] = "NO"
        child = subprocess.Popen(cmd, env=child_env)
        exit_code = child.wait()
    except KeyboardInterrupt:
        console.print("\n[yellow]STOP REQUESTED[/yellow]: stopping strategy and flattening Demo account...")
        exit_code = 130
        if child is not None and child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=2)
    except (MexcWebError, ValueError, IndexError) as exc:
        console.print(f"[red]LIVE DEMO LAUNCHER FAILED:[/red] {exc}")
        exit_code = 2
    except Exception as exc:
        console.print(f"[red]UNEXPECTED LIVE DEMO ERROR:[/red] {type(exc).__name__}: {exc}")
        exit_code = 3
    finally:
        cleanup_ok = _cleanup_sync("shutdown")
        if not cleanup_ok and exit_code == 0:
            exit_code = 4

    raise SystemExit(exit_code)


if __name__ == "__main__":
    main()
