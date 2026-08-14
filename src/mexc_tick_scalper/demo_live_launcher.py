from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from rich.console import Console
from rich.table import Table

from .demo_discovery import _fetch_contracts
from .demo_position_manager import flatten_all_demo_positions
from .execution import OrderSide
from .lead_lag import (
    BinanceBookTickerFeed,
    LeadLagModel,
    fetch_binance_usdm_symbols,
    mexc_to_binance_symbol,
)
from .market import MexcPublicMarket
from .web_execution import MexcWebError, MexcWebExecutionAdapter, WebExecutionConfig
from .web_fee import provider_from_web_fee_payload

console = Console()
LIVE_REST = "https://contract.mexc.com"
LIVE_WS = "wss://contract.mexc.com/edge"
SAMPLE_SECONDS = 10.0


@dataclass(slots=True)
class LeadLagSample:
    symbol: str
    ticks: int
    ready: int
    max_edge_bps: float
    max_binance_move_bps: float
    duration: float
    feed_error: str | None

    @property
    def tick_rate(self) -> float:
        return self.ticks / self.duration if self.duration > 0 else 0.0

    @property
    def ready_rate(self) -> float:
        return self.ready / self.duration if self.duration > 0 else 0.0


def _load_env() -> None:
    root = Path(__file__).resolve().parents[2]
    load_dotenv(root / ".env", override=False)


async def _sample_lead_lag(symbol: str, seconds: float = SAMPLE_SECONDS) -> LeadLagSample:
    market = MexcPublicMarket(LIVE_REST, LIVE_WS)
    model = LeadLagModel(
        horizon_ms=250,
        baseline_seconds=max(5.0, seconds * 0.75),
        min_edge_bps=4.0,
        min_binance_move_bps=1.0,
        max_age_ms=500.0,
    )
    feed = BinanceBookTickerFeed(symbol, model)
    start = time.monotonic()
    ticks = ready = 0
    max_edge = max_bmove = 0.0
    await feed.start()

    async def collect() -> None:
        nonlocal ticks, ready, max_edge, max_bmove
        async for tick in market.trades(symbol):
            ticks += 1
            now_ms = int(time.time() * 1000)
            model.update_mexc_price(price=tick.price, ts_ms=now_ms)
            snap = model.snapshot(now_ms=now_ms)
            max_edge = max(max_edge, abs(snap.edge_bps))
            max_bmove = max(max_bmove, abs(snap.binance_move_bps))
            if snap.ready:
                ready += 1
            if time.monotonic() - start >= seconds:
                break

    try:
        try:
            await asyncio.wait_for(collect(), timeout=seconds + 1.5)
        except TimeoutError:
            pass
    finally:
        await feed.close()

    return LeadLagSample(
        symbol=symbol,
        ticks=ticks,
        ready=ready,
        max_edge_bps=max_edge,
        max_binance_move_bps=max_bmove,
        duration=max(0.001, time.monotonic() - start),
        feed_error=feed.last_error,
    )


async def _candidates() -> list[dict[str, Any]]:
    try:
        binance_symbols = await fetch_binance_usdm_symbols()
    except Exception as exc:
        raise MexcWebError(f"cannot load Binance USD-M symbol universe: {type(exc).__name__}: {exc}") from exc

    cfg = WebExecutionConfig.demo_from_env(write_enabled=False)
    async with MexcWebExecutionAdapter(cfg) as adapter:
        contracts = await _fetch_contracts(adapter)
        fees = provider_from_web_fee_payload(await adapter.get_fee_rates())
        rows: list[dict[str, Any]] = []
        for row in contracts:
            symbol = str(row.get("symbol", "")).upper()
            if mexc_to_binance_symbol(symbol) not in binance_symbols:
                continue
            status = fees.status(symbol)
            if status.maker != 0 or status.taker != 0:
                continue
            try:
                ask = await adapter.get_best_price(symbol, OrderSide.LONG)
                bid = await adapter.get_best_price(symbol, OrderSide.SHORT)
            except MexcWebError:
                continue
            if ask <= 0 or bid <= 0 or ask < bid:
                continue
            item = dict(row)
            mid = (ask + bid) / 2.0
            item["spreadPct"] = ((ask - bid) / mid) * 100.0 if mid > 0 else 999.0
            rows.append(item)

    if not rows:
        return []

    console.print(
        f"Measuring Binance->MEXC lead-lag readiness for {len(rows)} cross-listed zero-fee Demo pair(s) "
        f"(~{int(SAMPLE_SECONDS)}s)..."
    )
    samples = await asyncio.gather(*(_sample_lead_lag(str(r.get("symbol", "")).upper()) for r in rows))
    sample_map = {sample.symbol: sample for sample in samples}

    for row in rows:
        sample = sample_map[str(row.get("symbol", "")).upper()]
        row["leadReady"] = sample.ready
        row["leadReadyRate"] = sample.ready_rate
        row["maxEdgeBps"] = sample.max_edge_bps
        row["maxBinanceMoveBps"] = sample.max_binance_move_bps
        row["liveTradeRate"] = sample.tick_rate
        row["binanceFeedError"] = sample.feed_error or ""

    rows.sort(
        key=lambda r: (
            -int(r.get("leadReady") or 0),
            -float(r.get("maxEdgeBps") or 0),
            float(r.get("spreadPct") or 999),
            -float(r.get("liveTradeRate") or 0),
        )
    )
    return rows


def _show(rows: list[dict[str, Any]]) -> None:
    table = Table(title="Zero-fee Binance/MEXC pairs ranked by live lead-lag readiness")
    table.add_column("#", justify="right")
    table.add_column("MEXC")
    table.add_column("Binance")
    table.add_column("LEADS", justify="right")
    table.add_column("LEADS/s", justify="right")
    table.add_column("Max edge bps", justify="right")
    table.add_column("Max B move", justify="right")
    table.add_column("MEXC trades/s", justify="right")
    table.add_column("Demo spread %", justify="right")
    for i, row in enumerate(rows, 1):
        symbol = str(row.get("symbol", "?"))
        table.add_row(
            str(i),
            symbol,
            mexc_to_binance_symbol(symbol),
            str(int(row.get("leadReady") or 0)),
            f"{float(row.get('leadReadyRate') or 0):.2f}",
            f"{float(row.get('maxEdgeBps') or 0):.2f}",
            f"{float(row.get('maxBinanceMoveBps') or 0):.2f}",
            f"{float(row.get('liveTradeRate') or 0):.2f}",
            f"{float(row.get('spreadPct') or 0):.4f}",
        )
    console.print(table)
    console.print(
        "Lead-lag scan: Binance USD-M bookTicker is the leader; MEXC LIVE trades are the lagger. "
        "Demo execution remains allowed only where maker/taker fee is exactly 0/0."
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
            "no MEXC Demo contract currently satisfies BOTH conditions: exact fee=0/0 and a matching "
            "TRADING Binance USD-M perpetual. Cross-exchange mode will not fake a leader symbol."
        )
    _show(rows)

    choice = input("Select pair number (or A for automatic best Binance->MEXC pair): ").strip().lower()
    selected = rows[0] if choice in {"", "a", "auto"} else rows[int(choice) - 1]

    symbol = str(selected.get("symbol", "")).upper()
    max_lev = int(selected.get("maxLeverage") or 1)
    leverage = max(1, min(_ask_int("Leverage", min(50, max_lev)), max_lev))
    cycles = _ask_int("Max cycles", 50)
    seconds = _ask_int("Max session seconds", 1800)
    margin = _ask_float("Target margin per IOC cycle, USDT", 2.0)

    console.print(
        f"Starting BINANCE-LEAD/MEXC-DEMO {symbol}: Binance={mexc_to_binance_symbol(symbol)} "
        f"scan_leads={int(selected.get('leadReady') or 0)} "
        f"max_edge={float(selected.get('maxEdgeBps') or 0):.2f}bps "
        f"Demo_spread={float(selected.get('spreadPct') or 0):.4f}%"
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
