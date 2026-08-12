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
from .hybrid_strategy import MicrostructureSignal
from .market import MexcPublicMarket
from .web_execution import MexcWebError, MexcWebExecutionAdapter, WebExecutionConfig
from .web_fee import provider_from_web_fee_payload

console = Console()
LIVE_REST = "https://contract.mexc.com"
LIVE_WS = "wss://contract.mexc.com/edge"
SAMPLE_SECONDS = 20.0
SIGNAL_WINDOW_SECONDS = 5.0
MIN_TRADE_RATE = 0.5
MIN_CONFIDENCE = 0.35
MIN_PRICE_CHANGES = 3


@dataclass(slots=True)
class LiveSample:
    symbol: str
    ticks: int
    price_changes: int
    ready_snapshots: int
    max_confidence: float
    duration: float

    @property
    def trade_rate(self) -> float:
        return self.ticks / self.duration if self.duration > 0 else 0.0

    @property
    def change_rate(self) -> float:
        return self.price_changes / self.duration if self.duration > 0 else 0.0

    @property
    def ready_rate(self) -> float:
        return self.ready_snapshots / self.duration if self.duration > 0 else 0.0


def _load_env() -> None:
    root = Path(__file__).resolve().parents[2]
    load_dotenv(root / ".env", override=False)


async def _sample_live(symbol: str, seconds: float = SAMPLE_SECONDS) -> LiveSample:
    market = MexcPublicMarket(LIVE_REST, LIVE_WS)
    signal = MicrostructureSignal(window_seconds=SIGNAL_WINDOW_SECONDS, min_trade_rate=MIN_TRADE_RATE)
    start = time.monotonic()
    ticks = 0
    changes = 0
    ready = 0
    max_conf = 0.0
    last_price: float | None = None

    async def collect() -> None:
        nonlocal ticks, changes, ready, max_conf, last_price
        async for tick in market.trades(symbol):
            ticks += 1
            if last_price is not None and tick.price != last_price:
                changes += 1
            last_price = tick.price

            snap = signal.update(tick)
            max_conf = max(max_conf, snap.confidence)
            if (
                snap.trade_rate >= MIN_TRADE_RATE
                and snap.price_changes >= MIN_PRICE_CHANGES
                and snap.direction != 0
                and snap.confidence >= MIN_CONFIDENCE
            ):
                ready += 1

            if time.monotonic() - start >= seconds:
                break

    try:
        await asyncio.wait_for(collect(), timeout=seconds + 2.0)
    except TimeoutError:
        pass

    return LiveSample(
        symbol=symbol,
        ticks=ticks,
        price_changes=changes,
        ready_snapshots=ready,
        max_confidence=max_conf,
        duration=max(0.001, time.monotonic() - start),
    )


async def _candidates() -> list[dict[str, Any]]:
    cfg = WebExecutionConfig.demo_from_env(write_enabled=False)
    async with MexcWebExecutionAdapter(cfg) as adapter:
        contracts = await _fetch_contracts(adapter)
        fees = provider_from_web_fee_payload(await adapter.get_fee_rates())
        rows: list[dict[str, Any]] = []
        for row in contracts:
            symbol = str(row.get("symbol", "")).upper()
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
            item["spreadPct"] = ((ask - bid) / ((ask + bid) / 2.0)) * 100.0
            rows.append(item)

    console.print(
        f"Measuring LIVE MEXC signal readiness for {len(rows)} zero-fee Demo-compatible pairs "
        f"({int(SAMPLE_SECONDS)}s sample)..."
    )
    samples = await asyncio.gather(*(_sample_live(str(r.get("symbol", "")).upper()) for r in rows))
    sample_map = {s.symbol: s for s in samples}
    for row in rows:
        s = sample_map[str(row.get("symbol", "")).upper()]
        row["liveTicks"] = s.ticks
        row["liveTradeRate"] = s.trade_rate
        row["liveChangeRate"] = s.change_rate
        row["readySnapshots"] = s.ready_snapshots
        row["readyRate"] = s.ready_rate
        row["maxConfidence"] = s.max_confidence

    rows.sort(
        key=lambda r: (
            -int(r.get("readySnapshots") or 0),
            -float(r.get("readyRate") or 0),
            -float(r.get("liveChangeRate") or 0),
            -float(r.get("liveTradeRate") or 0),
            float(r.get("spreadPct") or 999),
        )
    )
    return rows


def _show(rows: list[dict[str, Any]]) -> None:
    table = Table(title="Zero-fee Demo pairs ranked by actual Hybrid READY signals")
    table.add_column("#", justify="right")
    table.add_column("Symbol")
    table.add_column("READY", justify="right")
    table.add_column("READY/s", justify="right")
    table.add_column("LIVE chg/s", justify="right")
    table.add_column("Trades/s", justify="right")
    table.add_column("Max conf", justify="right")
    table.add_column("Demo spread %", justify="right")
    table.add_column("Max lev", justify="right")
    for i, row in enumerate(rows, 1):
        table.add_row(
            str(i),
            str(row.get("symbol", "?")),
            str(int(row.get("readySnapshots") or 0)),
            f"{float(row.get('readyRate') or 0):.2f}",
            f"{float(row.get('liveChangeRate') or 0):.2f}",
            f"{float(row.get('liveTradeRate') or 0):.2f}",
            f"{float(row.get('maxConfidence') or 0):.3f}",
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


async def _prepare_session() -> tuple[str, int, int, int, float]:
    await flatten_all_demo_positions(reason="startup")

    rows = await _candidates()
    if not rows:
        raise MexcWebError("no confirmed zero-fee Demo-compatible contracts")
    _show(rows)

    ready_rows = [r for r in rows if int(r.get("readySnapshots") or 0) > 0]
    if not ready_rows:
        raise MexcWebError(
            "none of the zero-fee Demo-compatible pairs produced a valid Hybrid READY signal during the sample; "
            "not starting a session that would only sit BLOCKED"
        )

    choice = input("Select pair number (or A for automatic best READY pair): ").strip().lower()
    selected = ready_rows[0] if choice in {"", "a", "auto"} else rows[int(choice) - 1]
    if int(selected.get("readySnapshots") or 0) <= 0:
        raise MexcWebError(f"{selected.get('symbol')} produced no valid Hybrid READY signal during the sample")

    symbol = str(selected.get("symbol", "")).upper()
    max_lev = int(selected.get("maxLeverage") or 1)
    leverage = max(1, min(_ask_int("Leverage", min(50, max_lev)), max_lev))
    cycles = _ask_int("Max cycles", 50)
    seconds = _ask_int("Max session seconds", 1800)
    margin = _ask_float("Target margin per IOC cycle, USDT", 2.0)

    console.print(
        f"Starting LIVE-SIGNAL/DEMO-EXEC {symbol}: "
        f"READY={int(selected.get('readySnapshots') or 0)} "
        f"ready_rate={float(selected.get('readyRate') or 0):.2f}/s "
        f"max_conf={float(selected.get('maxConfidence') or 0):.3f}"
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
            "mexc_tick_scalper.demo_live_signal_test",
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
        child_env["MEXC_DEMO_AUTO_FLATTEN_START"] = "YES"
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
