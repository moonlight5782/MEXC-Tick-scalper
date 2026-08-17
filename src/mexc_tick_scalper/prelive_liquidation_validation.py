from __future__ import annotations

import argparse
import asyncio
import csv
import json
import math
import time
from dataclasses import dataclass
from pathlib import Path

import aiohttp
from rich.console import Console

from . import prelive_persistent_ioc_shadow_v2 as v2
from .baseline_v1 import apply_baseline_v1
from .margin_liquidation_replay import fetch_contract_risk, liquidation_distance_bps, liquidation_price
from .persistent_lag_profile import build_profiles, latest_lifetime_csv, select_profiles

console = Console()
MEXC_WS = "wss://contract.mexc.com/edge"


class TargetClosedTradesReached(RuntimeError):
    pass


@dataclass(slots=True)
class ClosedTrade:
    index: int
    symbol: str
    direction: int
    entry_ts_ms: int
    exit_ts_ms: int
    entry_price: float
    recorded_notional: float
    logged_pnl_usdt: float
    logged_pnl_bps: float
    exit_reason: str


@dataclass(slots=True)
class FairTick:
    ts_ms: int
    symbol: str
    price: float


def _selected_symbols(args: argparse.Namespace) -> list[str]:
    source = Path(args.lifetime_csv) if args.lifetime_csv else latest_lifetime_csv(Path.cwd())
    profiles = select_profiles(
        build_profiles(source),
        min_signals=args.pair_min_signals,
        min_median_lifetime_ms=args.pair_min_median_lifetime_ms,
        min_survival_rate=args.pair_min_survival_rate,
        min_signal_strength_ratio=args.pair_min_strength_ratio,
    )
    return sorted({p.symbol for p in profiles})


async def _fair_price_loop(symbols: list[str], stop: asyncio.Event, rows: list[FairTick], csv_path: Path) -> None:
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    with csv_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["ts_ms", "symbol", "fair_price"])
        while not stop.is_set():
            try:
                timeout = aiohttp.ClientTimeout(total=None, sock_connect=15, sock_read=None)
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    async with session.ws_connect(MEXC_WS, heartbeat=20) as ws:
                        for symbol in symbols:
                            await ws.send_json({"method": "sub.fair.price", "param": {"symbol": symbol}, "gzip": False})
                        last_ping = time.monotonic()
                        while not stop.is_set():
                            if time.monotonic() - last_ping >= 10.0:
                                await ws.send_json({"method": "ping"})
                                last_ping = time.monotonic()
                            try:
                                msg = await asyncio.wait_for(ws.receive(), timeout=1.0)
                            except TimeoutError:
                                continue
                            if msg.type == aiohttp.WSMsgType.TEXT:
                                payload = json.loads(msg.data)
                                if payload.get("channel") != "push.fair.price":
                                    continue
                                data = payload.get("data") or {}
                                symbol = str(data.get("symbol") or payload.get("symbol") or "").upper()
                                price = float(data.get("price") or 0.0)
                                ts_ms = int(payload.get("ts") or data.get("timestamp") or time.time() * 1000)
                                if symbol and price > 0:
                                    tick = FairTick(ts_ms=ts_ms, symbol=symbol, price=price)
                                    rows.append(tick)
                                    writer.writerow([tick.ts_ms, tick.symbol, f"{tick.price:.12g}"])
                                    fh.flush()
                            elif msg.type in {aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.ERROR}:
                                break
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                console.print(f"[yellow]Fair-price WS reconnect after error:[/yellow] {exc}")
                try:
                    await asyncio.wait_for(stop.wait(), timeout=1.0)
                except TimeoutError:
                    pass


def _fair_path_for_trade(ticks_by_symbol: dict[str, list[FairTick]], trade: ClosedTrade) -> list[FairTick]:
    ticks = ticks_by_symbol.get(trade.symbol, [])
    if not ticks:
        return []
    before: FairTick | None = None
    path: list[FairTick] = []
    for tick in ticks:
        if tick.ts_ms <= trade.entry_ts_ms:
            before = tick
            continue
        if tick.ts_ms > trade.exit_ts_ms:
            break
        path.append(tick)
    if before is not None and trade.entry_ts_ms - before.ts_ms <= 2000:
        path.insert(0, before)
    return path


def _build_liquidation_report(
    trades: list[ClosedTrade],
    fair_ticks: list[FairTick],
    risks,
    *,
    starting_balance_usdt: float,
    margin_fraction: float,
    requested_leverage: int,
    liquidation_fee_rate: float,
    report_path: Path,
) -> tuple[float, int, int]:
    ticks_by_symbol: dict[str, list[FairTick]] = {}
    for tick in fair_ticks:
        ticks_by_symbol.setdefault(tick.symbol, []).append(tick)
    for ticks in ticks_by_symbol.values():
        ticks.sort(key=lambda x: x.ts_ms)

    balance = float(starting_balance_usdt)
    liquidations = 0
    missing_fair = 0
    gross_win = 0.0
    gross_loss = 0.0
    report_path.parent.mkdir(parents=True, exist_ok=True)

    fields = [
        "index", "symbol", "side", "entry_ts_ms", "exit_ts_ms", "entry_price",
        "leverage", "mmr", "recorded_notional", "sim_notional", "initial_margin",
        "liq_distance_bps", "liq_price", "fair_ticks", "adverse_fair_price", "adverse_fair_bps",
        "liquidated", "exit_reason", "logged_pnl_bps", "sim_pnl_usdt", "balance_after",
    ]
    with report_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        for trade in trades:
            if balance <= 0:
                break
            risk = risks.get(trade.symbol)
            if risk is None:
                raise KeyError(f"Missing MEXC contract risk data for {trade.symbol}")
            leverage = risk.max_leverage if requested_leverage <= 0 else min(requested_leverage, risk.max_leverage)
            leverage = max(1, leverage)
            margin_budget = balance * margin_fraction
            sim_notional = min(trade.recorded_notional, margin_budget * leverage)
            initial_margin = sim_notional / leverage
            liq_dist = liquidation_distance_bps(leverage, risk.maintenance_margin_rate, liquidation_fee_rate)
            liq_price = liquidation_price(
                trade.entry_price, trade.direction, leverage, risk.maintenance_margin_rate, liquidation_fee_rate
            )

            path = _fair_path_for_trade(ticks_by_symbol, trade)
            if not path:
                missing_fair += 1
                adverse_price = math.nan
                adverse_bps = math.nan
                liquidated = False
            else:
                adverse_price = min(x.price for x in path) if trade.direction > 0 else max(x.price for x in path)
                adverse_bps = trade.direction * (adverse_price / trade.entry_price - 1.0) * 10_000.0
                liquidated = adverse_price <= liq_price if trade.direction > 0 else adverse_price >= liq_price

            if liquidated:
                liquidations += 1
                pnl = -sim_notional * liq_dist / 10_000.0
            else:
                pnl = sim_notional * trade.logged_pnl_bps / 10_000.0
            balance = max(0.0, balance + pnl)
            gross_win += max(0.0, pnl)
            gross_loss += max(0.0, -pnl)

            writer.writerow({
                "index": trade.index,
                "symbol": trade.symbol,
                "side": "LONG" if trade.direction > 0 else "SHORT",
                "entry_ts_ms": trade.entry_ts_ms,
                "exit_ts_ms": trade.exit_ts_ms,
                "entry_price": f"{trade.entry_price:.12g}",
                "leverage": leverage,
                "mmr": f"{risk.maintenance_margin_rate:.8f}",
                "recorded_notional": f"{trade.recorded_notional:.8f}",
                "sim_notional": f"{sim_notional:.8f}",
                "initial_margin": f"{initial_margin:.8f}",
                "liq_distance_bps": f"{liq_dist:.6f}",
                "liq_price": f"{liq_price:.12g}",
                "fair_ticks": len(path),
                "adverse_fair_price": "" if math.isnan(adverse_price) else f"{adverse_price:.12g}",
                "adverse_fair_bps": "" if math.isnan(adverse_bps) else f"{adverse_bps:.6f}",
                "liquidated": int(liquidated),
                "exit_reason": trade.exit_reason,
                "logged_pnl_bps": f"{trade.logged_pnl_bps:.6f}",
                "sim_pnl_usdt": f"{pnl:.8f}",
                "balance_after": f"{balance:.8f}",
            })

    pf = math.inf if gross_loss == 0 and gross_win > 0 else gross_win / gross_loss if gross_loss > 0 else 0.0
    pf_text = "inf" if math.isinf(pf) else f"{pf:.3f}"
    console.print(
        f"[bold cyan]LIQUIDATION-AWARE RESULT[/bold cyan] start=${starting_balance_usdt:.2f} "
        f"final=${balance:.2f} pnl=${balance-starting_balance_usdt:+.2f} PF={pf_text} "
        f"liquidations={liquidations}/{len(trades)} fair_path_missing={missing_fair}/{len(trades)}"
    )
    return balance, liquidations, missing_fair


async def run(args: argparse.Namespace) -> None:
    apply_baseline_v1(args)
    target = max(1, int(args.target_closed_trades))
    symbols = _selected_symbols(args)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    fair_csv = Path(args.fair_csv or f"fair_price_trace_{stamp}.csv")
    report_csv = Path(args.report_csv or f"liquidation_validation_{stamp}.csv")

    console.print("[bold cyan]BASELINE V1 + REAL MEXC FAIR-PRICE LIQUIDATION VALIDATION[/bold cyan]")
    console.print(f"NO REAL ORDERS. bank=${args.balance_usdt:.2f}; symbols={len(symbols)}; target_closed={target}")
    console.print(f"Fair-price trace: {fair_csv}")
    console.print(f"Final liquidation report: {report_csv}")

    fair_rows: list[FairTick] = []
    closed_trades: list[ClosedTrade] = []
    fair_stop = asyncio.Event()
    fair_task = asyncio.create_task(_fair_price_loop(symbols, fair_stop, fair_rows, fair_csv))

    original_close = v2._close_trade

    def close_capture(stats: v2.Stats, pos: v2.Position, now_ms: int) -> None:
        pnl_bps = pos.realized_pnl_usdt / max(pos.entry_notional, 1e-12) * 10_000.0
        closed_trades.append(ClosedTrade(
            index=len(closed_trades) + 1,
            symbol=pos.signal.symbol,
            direction=pos.signal.direction,
            entry_ts_ms=pos.entry_ts_ms,
            exit_ts_ms=now_ms,
            entry_price=pos.entry_price,
            recorded_notional=pos.entry_notional,
            logged_pnl_usdt=pos.realized_pnl_usdt,
            logged_pnl_bps=pnl_bps,
            exit_reason=pos.exit_reason or "unknown",
        ))
        original_close(stats, pos, now_ms)
        if len(closed_trades) >= target:
            raise TargetClosedTradesReached

    v2._close_trade = close_capture
    runner_error: BaseException | None = None
    try:
        await v2.run(args)
    except TargetClosedTradesReached:
        console.print(f"[green]Reached exact closed-trade target: {target}[/green]")
    except BaseException as exc:
        runner_error = exc
        console.print(f"[red]Main runner stopped after {len(closed_trades)} closed trades:[/red] {type(exc).__name__}: {exc}")
    finally:
        v2._close_trade = original_close
        fair_stop.set()
        try:
            await asyncio.wait_for(fair_task, timeout=3.0)
        except (TimeoutError, asyncio.CancelledError):
            fair_task.cancel()

    if closed_trades:
        risks = await fetch_contract_risk()
        _build_liquidation_report(
            closed_trades,
            fair_rows,
            risks,
            starting_balance_usdt=args.balance_usdt,
            margin_fraction=args.margin_fraction,
            requested_leverage=args.leverage,
            liquidation_fee_rate=args.liquidation_fee_rate,
            report_path=report_csv,
        )
    else:
        console.print("[red]No closed trades were captured; liquidation report was not generated.[/red]")

    if runner_error is not None:
        console.print("[yellow]Partial results above were saved despite the runner error.[/yellow]")


def build_parser() -> argparse.ArgumentParser:
    p = v2.build_parser()
    p.description = "Frozen baseline-v1 LIVE paper validation with real MEXC fair-price liquidation tracking"
    p.add_argument("--target-closed-trades", type=int, default=100)
    p.add_argument("--balance-usdt", type=float, default=100.0)
    p.add_argument("--margin-fraction", type=float, default=1.0, help="Fraction of current bank available as isolated margin")
    p.add_argument("--leverage", type=int, default=0, help="0 = current MEXC max leverage per contract")
    p.add_argument("--liquidation-fee-rate", type=float, default=0.0)
    p.add_argument("--fair-csv", default="")
    p.add_argument("--report-csv", default="")
    return p


def main() -> None:
    args = build_parser().parse_args()
    if args.balance_usdt <= 0:
        raise SystemExit("--balance-usdt must be > 0")
    if not 0 < args.margin_fraction <= 1:
        raise SystemExit("--margin-fraction must be in (0, 1]")
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
