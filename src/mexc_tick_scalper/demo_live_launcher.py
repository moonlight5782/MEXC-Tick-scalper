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
SAMPLE_SECONDS = 25.0
MIN_CONFIDENCE = 0.35


@dataclass(frozen=True, slots=True)
class SignalProfile:
    name: str
    window_seconds: float
    min_trade_rate: float
    min_price_changes: int
    rank: int


PROFILES = (
    SignalProfile("STRICT", 5.0, 0.50, 3, 0),
    SignalProfile("BALANCED", 10.0, 0.30, 2, 1),
    SignalProfile("SLOW", 15.0, 0.15, 2, 2),
)


@dataclass(slots=True)
class LiveSample:
    symbol: str
    ticks: int
    price_changes: int
    ready_by_profile: dict[str, int]
    max_confidence: float
    duration: float

    @property
    def trade_rate(self) -> float:
        return self.ticks / self.duration if self.duration > 0 else 0.0

    @property
    def change_rate(self) -> float:
        return self.price_changes / self.duration if self.duration > 0 else 0.0

    def best_profile(self) -> SignalProfile | None:
        for profile in PROFILES:
            if self.ready_by_profile.get(profile.name, 0) > 0:
                return profile
        return None

    def ready_count(self) -> int:
        profile = self.best_profile()
        return self.ready_by_profile.get(profile.name, 0) if profile else 0

    def ready_rate(self) -> float:
        return self.ready_count() / self.duration if self.duration > 0 else 0.0


def _load_env() -> None:
    root = Path(__file__).resolve().parents[2]
    load_dotenv(root / ".env", override=False)


async def _sample_live(symbol: str, seconds: float = SAMPLE_SECONDS) -> LiveSample:
    market = MexcPublicMarket(LIVE_REST, LIVE_WS)
    signals = {
        profile.name: MicrostructureSignal(
            window_seconds=profile.window_seconds,
            min_trade_rate=profile.min_trade_rate,
        )
        for profile in PROFILES
    }
    start = time.monotonic()
    ticks = 0
    changes = 0
    ready = {profile.name: 0 for profile in PROFILES}
    max_conf = 0.0
    last_price: float | None = None

    async def collect() -> None:
        nonlocal ticks, changes, max_conf, last_price
        async for tick in market.trades(symbol):
            ticks += 1
            if last_price is not None and tick.price != last_price:
                changes += 1
            last_price = tick.price

            for profile in PROFILES:
                snap = signals[profile.name].update(tick)
                max_conf = max(max_conf, snap.confidence)
                if (
                    snap.trade_rate >= profile.min_trade_rate
                    and snap.price_changes >= profile.min_price_changes
                    and snap.direction != 0
                    and snap.confidence >= MIN_CONFIDENCE
                ):
                    ready[profile.name] += 1

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
        ready_by_profile=ready,
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
        f"Measuring LIVE MEXC readiness for {len(rows)} zero-fee Demo-compatible pairs "
        f"({int(SAMPLE_SECONDS)}s, adaptive profiles)..."
    )
    samples = await asyncio.gather(*(_sample_live(str(r.get("symbol", "")).upper()) for r in rows))
    sample_map = {s.symbol: s for s in samples}

    for row in rows:
        s = sample_map[str(row.get("symbol", "")).upper()]
        profile = s.best_profile()
        row["liveTicks"] = s.ticks
        row["liveTradeRate"] = s.trade_rate
        row["liveChangeRate"] = s.change_rate
        row["readySnapshots"] = s.ready_count()
        row["readyRate"] = s.ready_rate()
        row["maxConfidence"] = s.max_confidence
        row["signalProfile"] = profile.name if profile else "NONE"
        row["profileRank"] = profile.rank if profile else 99
        row["signalWindow"] = profile.window_seconds if profile else 0.0
        row["signalMinRate"] = profile.min_trade_rate if profile else 0.0
        row["signalMinChanges"] = profile.min_price_changes if profile else 0

    rows.sort(
        key=lambda r: (
            int(r.get("profileRank") or 99),
            -int(r.get("readySnapshots") or 0),
            -float(r.get("readyRate") or 0),
            -float(r.get("maxConfidence") or 0),
            float(r.get("spreadPct") or 999),
        )
    )
    return rows


def _show(rows: list[dict[str, Any]]) -> None:
    table = Table(title="Zero-fee Demo pairs ranked by adaptive Hybrid readiness")
    table.add_column("#", justify="right")
    table.add_column("Symbol")
    table.add_column("Profile")
    table.add_column("READY", justify="right")
    table.add_column("READY/s", justify="right")
    table.add_column("LIVE chg/s", justify="right")
    table.add_column("Trades/s", justify="right")
    table.add_column("Max conf", justify="right")
    table.add_column("Demo spread %", justify="right")
    for i, row in enumerate(rows, 1):
        table.add_row(
            str(i),
            str(row.get("symbol", "?")),
            str(row.get("signalProfile", "NONE")),
            str(int(row.get("readySnapshots") or 0)),
            f"{float(row.get('readyRate') or 0):.2f}",
            f"{float(row.get('liveChangeRate') or 0):.2f}",
            f"{float(row.get('liveTradeRate') or 0):.2f}",
            f"{float(row.get('maxConfidence') or 0):.3f}",
            f"{float(row.get('spreadPct') or 0):.4f}",
        )
    console.print(table)
    console.print(
        "Profiles: STRICT=5s/3 changes/rate>=0.50, "
        "BALANCED=10s/2 changes/rate>=0.30, SLOW=15s/2 changes/rate>=0.15. "
        "Confidence remains >=0.35 in every profile."
    )


def _ask_int(prompt: str, default: int) -> int:
    raw = input(f"{prompt} [{default}]: ").strip()
    return default if not raw else int(raw)


def _ask_float(prompt: str, default: float) -> float:
    raw = input(f"{prompt} [{default:g}]: ").strip()
    return default if not raw else float(raw)


async def _prepare_session() -> tuple[str, int, int, int, float, float, float, int]:
    await flatten_all_demo_positions(reason="startup")

    rows = await _candidates()
    if not rows:
        raise MexcWebError("no confirmed zero-fee Demo-compatible contracts")
    _show(rows)

    ready_rows = [r for r in rows if int(r.get("readySnapshots") or 0) > 0]
    if not ready_rows:
        raise MexcWebError(
            "no zero-fee Demo-compatible pair produced a valid signal even with the safe SLOW profile; "
            "market is too inactive right now"
        )

    choice = input("Select pair number (or A for automatic best profile/pair): ").strip().lower()
    selected = ready_rows[0] if choice in {"", "a", "auto"} else rows[int(choice) - 1]
    if int(selected.get("readySnapshots") or 0) <= 0:
        raise MexcWebError(f"{selected.get('symbol')} produced no valid Hybrid signal during the sample")

    symbol = str(selected.get("symbol", "")).upper()
    max_lev = int(selected.get("maxLeverage") or 1)
    leverage = max(1, min(_ask_int("Leverage", min(50, max_lev)), max_lev))
    cycles = _ask_int("Max cycles", 50)
    seconds = _ask_int("Max session seconds", 1800)
    margin = _ask_float("Target margin per IOC cycle, USDT", 2.0)
    signal_window = float(selected.get("signalWindow") or 5.0)
    min_rate = float(selected.get("signalMinRate") or 0.5)
    min_changes = int(selected.get("signalMinChanges") or 3)

    console.print(
        f"Starting LIVE-SIGNAL/DEMO-EXEC {symbol}: profile={selected.get('signalProfile')} "
        f"READY={int(selected.get('readySnapshots') or 0)} "
        f"ready_rate={float(selected.get('readyRate') or 0):.2f}/s "
        f"max_conf={float(selected.get('maxConfidence') or 0):.3f} "
        f"window={signal_window:g}s min_rate={min_rate:g}/s min_changes={min_changes}"
    )
    return symbol, leverage, cycles, seconds, margin, signal_window, min_rate, min_changes


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
        symbol, leverage, cycles, seconds, margin, signal_window, min_rate, min_changes = asyncio.run(_prepare_session())
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
            "--signal-window-seconds",
            str(signal_window),
            "--min-trade-rate",
            str(min_rate),
            "--min-price-changes",
            str(min_changes),
            "--min-confidence",
            str(MIN_CONFIDENCE),
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
