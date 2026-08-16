from __future__ import annotations

import argparse
import asyncio
import csv
import math
import os
import time
import uuid
from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv
from rich.console import Console

from .execution import OrderFill, OrderSide, PositionSnapshot
from .live_lead_lag_shadow import PositiveTrailing
from .live_zero_fee_universe import discover_live_zero_fee_crosslisted
from .microspread import MicroSpreadModel
from .microspread_feed import EventBinanceBookTickerFeed, EventMexcDepthFeed, LiveBook
from .web_execution import LIVE_FUTURES_HOST, MexcWebError, MexcWebExecutionAdapter, WebExecutionConfig
from .web_fee import read_web_fee_provider

console = Console()
FEE_REFRESH_SECONDS = 2.0
FEE_MAX_AGE_MS = 3_500


@dataclass(frozen=True, slots=True)
class Candidate:
    symbol: str
    direction: int
    edge_bps: float
    threshold_bps: float
    spread_bps: float
    net_margin_bps: float
    book: LiveBook


@dataclass(slots=True)
class FeeCache:
    provider: object | None = None
    checked_ms: int = 0
    last_error: str = "not_started"

    def fresh_zero(self, symbol: str, now_ms: int) -> bool:
        if self.provider is None or now_ms - self.checked_ms > FEE_MAX_AGE_MS:
            return False
        status = self.provider.status(symbol)
        return status.maker == 0 and status.taker == 0


@dataclass(slots=True)
class LivePosition:
    remote: PositionSnapshot
    direction: int
    entry_edge_bps: float
    entry_spread_bps: float
    opened_monotonic: float
    entry_ms: int
    entry_fill: OrderFill
    trailing: PositiveTrailing
    mfe_bps: float = 0.0
    mae_bps: float = 0.0


def _load_env() -> None:
    root = Path(__file__).resolve().parents[2]
    load_dotenv(root / ".env", override=False)


def _assert_live_write_config(cfg: WebExecutionConfig) -> None:
    if cfg.environment != "live":
        raise MexcWebError(f"LIVE runner requires environment=live, got {cfg.environment!r}")
    host = (urlparse(cfg.base_url).hostname or "").lower()
    if host != LIVE_FUTURES_HOST:
        raise MexcWebError(f"LIVE runner refuses host {host!r}; expected {LIVE_FUTURES_HOST!r}")
    if not cfg.write_enabled:
        raise MexcWebError("LIVE runner requires write_enabled=True")
    if os.getenv("MEXC_LIVE_WRITE", "").strip().upper() != "YES":
        raise MexcWebError("LIVE writes are locked. Set MEXC_LIVE_WRITE=YES only when intentionally starting real trading.")


def _required_edge(spread_bps: float, args: argparse.Namespace) -> float:
    return max(
        float(args.min_edge_bps),
        float(spread_bps) + float(args.min_net_edge_bps),
        float(spread_bps) * float(args.edge_to_spread_ratio),
    )


def _marketable_ioc_price(side: OrderSide, book: LiveBook, cross_bps: float, price_unit: float) -> float:
    best = book.ask if side is OrderSide.LONG else book.bid
    if best <= 0 or price_unit <= 0:
        raise MexcWebError("invalid live book or priceUnit")
    cross = Decimal(str(max(0.0, cross_bps))) / Decimal("10000")
    factor = Decimal("1") + cross if side is OrderSide.LONG else Decimal("1") - cross
    raw = Decimal(str(best)) * factor
    tick = Decimal(str(price_unit))
    rounding = ROUND_CEILING if side is OrderSide.LONG else ROUND_FLOOR
    return float((raw / tick).to_integral_value(rounding=rounding) * tick)


def _signed_move_bps(direction: int, entry: float, current: float) -> float:
    if direction not in (-1, 1) or entry <= 0 or current <= 0:
        return 0.0
    return direction * (current - entry) / entry * 10_000.0


def _candidate(
    symbol: str,
    model: MicroSpreadModel,
    book: LiveBook,
    args: argparse.Namespace,
    now_ms: int,
    *,
    consume: bool,
) -> Candidate | None:
    threshold = _required_edge(book.spread_bps, args)
    snap = model.signal(now_ms=now_ms, threshold_bps=threshold) if consume else model.snapshot(
        now_ms=now_ms,
        threshold_bps=threshold,
    )
    if not snap.ready:
        return None
    edge_abs = abs(float(snap.edge_bps))
    return Candidate(
        symbol=symbol,
        direction=int(snap.direction),
        edge_bps=float(snap.edge_bps),
        threshold_bps=float(threshold),
        spread_bps=float(book.spread_bps),
        net_margin_bps=edge_abs - float(threshold),
        book=book,
    )


def _choose_best(rows: list[Candidate]) -> Candidate | None:
    if not rows:
        return None
    return max(rows, key=lambda row: (row.net_margin_bps, abs(row.edge_bps), -row.spread_bps))


def _append_trade(path: Path, row: dict[str, object]) -> None:
    fields = [
        "entry_ms", "exit_ms", "symbol", "direction", "qty", "leverage", "entry_price", "exit_price",
        "entry_edge_bps", "entry_spread_bps", "pnl_bps", "pnl_usdt", "mfe_bps", "mae_bps", "hold_ms",
        "exit_reason", "entry_fee_usdt", "exit_fee_usdt", "entry_order_id", "exit_order_id",
    ]
    exists = path.exists()
    with path.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        if not exists:
            writer.writeheader()
        writer.writerow({name: row.get(name, "") for name in fields})


async def _fee_loop(cache: FeeCache, stop: asyncio.Event) -> None:
    cfg = WebExecutionConfig.from_env(write_enabled=False)
    async with MexcWebExecutionAdapter(cfg) as adapter:
        while not stop.is_set():
            try:
                cache.provider = await read_web_fee_provider(adapter)
                cache.checked_ms = int(time.time() * 1000)
                cache.last_error = ""
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                cache.checked_ms = 0
                cache.last_error = f"{type(exc).__name__}: {exc}"
            try:
                await asyncio.wait_for(stop.wait(), timeout=FEE_REFRESH_SECONDS)
            except TimeoutError:
                pass


async def _resolve_remote_position(
    adapter: MexcWebExecutionAdapter,
    symbol: str,
    side: OrderSide,
    fill: OrderFill,
    leverage: int,
) -> PositionSnapshot:
    if fill.filled_qty <= 0:
        raise MexcWebError("IOC returned no fill")
    provisional = PositionSnapshot(
        symbol=symbol,
        side=side,
        qty=fill.filled_qty,
        entry_price=fill.avg_price,
        leverage=leverage,
        isolated=True,
        position_id=fill.position_id,
    )
    deadline = time.monotonic() + 0.75
    while time.monotonic() < deadline:
        rows = await adapter.get_positions(symbol)
        matching = next((row for row in rows if row.side is side), None)
        if matching is not None:
            return matching
        await asyncio.sleep(0.025)
    return provisional


async def _submit_close(adapter: MexcWebExecutionAdapter, position: PositionSnapshot) -> OrderFill:
    client_id = f"live-exit-{uuid.uuid4().hex}"[:32]
    if position.position_id:
        return await adapter.close_position_snapshot_reduce_only(position, client_order_id=client_id)
    return await adapter.close_market_reduce_only(
        symbol=position.symbol,
        qty=position.qty,
        side=OrderSide.SHORT if position.side is OrderSide.LONG else OrderSide.LONG,
        client_order_id=client_id,
    )


async def _find_same_position(
    adapter: MexcWebExecutionAdapter,
    position: PositionSnapshot,
) -> PositionSnapshot | None:
    rows = await adapter.get_positions(position.symbol)
    if position.position_id is not None:
        return next((row for row in rows if row.position_id == position.position_id), None)
    return next((row for row in rows if row.side is position.side), None)


async def _close_position_fully(
    adapter: MexcWebExecutionAdapter,
    position: PositionSnapshot,
    *,
    attempts: int = 4,
) -> OrderFill:
    current = position
    last_fill: OrderFill | None = None
    for _ in range(max(1, attempts)):
        last_fill = await _submit_close(adapter, current)
        deadline = time.monotonic() + 0.75
        while time.monotonic() < deadline:
            residual = await _find_same_position(adapter, current)
            if residual is None:
                return last_fill
            current = residual
            await asyncio.sleep(0.04)
    residual = await _find_same_position(adapter, current)
    if residual is not None:
        raise MexcWebError(
            f"LIVE reduce-only close left residual position {residual.symbol} "
            f"positionId={residual.position_id} qty={residual.qty:g}"
        )
    if last_fill is None:
        raise MexcWebError("LIVE close did not submit")
    return last_fill


async def _emergency_close(adapter: MexcWebExecutionAdapter, position: LivePosition, reason: str) -> None:
    console.print(f"[bold red]EMERGENCY LIVE CLOSE[/bold red] {position.remote.symbol} reason={reason}")
    await asyncio.wait_for(
        asyncio.shield(_close_position_fully(adapter, position.remote)),
        timeout=8.0,
    )


async def run(args: argparse.Namespace) -> None:
    _load_env()
    if args.confirm_live != "LIVE":
        raise MexcWebError("Real trading requires --confirm-live LIVE")

    contracts = await discover_live_zero_fee_crosslisted()
    included = {item.strip().upper() for item in args.include_symbols.split(",") if item.strip()}
    excluded = {item.strip().upper() for item in args.exclude_symbols.split(",") if item.strip()}
    if included:
        contracts = [row for row in contracts if row.mexc_symbol in included]
    if excluded:
        contracts = [row for row in contracts if row.mexc_symbol not in excluded]
    if not contracts:
        raise MexcWebError("No currently exact-0/0 LIVE Binance-crosslisted symbols remain after filters")

    symbols = [row.mexc_symbol for row in contracts]
    models = {
        row.mexc_symbol: MicroSpreadModel(
            horizon_ms=args.micro_horizon_ms,
            baseline_seconds=args.baseline_seconds,
            baseline_exclusion_ms=args.baseline_exclusion_ms,
            min_edge_bps=args.min_edge_bps,
            min_binance_move_bps=args.min_binance_move_bps,
            max_binance_age_ms=args.max_binance_age_ms,
            max_mexc_age_ms=args.max_mexc_age_ms,
            rearm_fraction=args.rearm_fraction,
        )
        for row in contracts
    }
    wake = asyncio.Event()
    binance = EventBinanceBookTickerFeed(contracts, models, wake)
    mexc = EventMexcDepthFeed(symbols, models, wake, depth_limit=5)
    await binance.start()
    await mexc.start()

    fee_cache = FeeCache()
    fee_stop = asyncio.Event()
    fee_task = asyncio.create_task(_fee_loop(fee_cache, fee_stop))

    cfg = WebExecutionConfig.from_env(write_enabled=True)
    _assert_live_write_config(cfg)
    log_path = Path(args.trade_csv) if args.trade_csv else Path(f"live_trades_{int(time.time())}.csv")
    contract_by_symbol: dict[str, tuple[float, int, float, float]] = {}
    position: LivePosition | None = None
    cycles = 0
    realized_pnl_bps = 0.0
    realized_pnl_usdt = 0.0
    peak_realized_usdt = 0.0
    max_drawdown_usdt = 0.0
    next_heartbeat = 0.0
    warmup_until = time.monotonic() + args.warmup_seconds
    deadline = time.monotonic() + args.session_seconds
    last_entry_ms = {symbol: -10**18 for symbol in symbols}

    try:
        async with MexcWebExecutionAdapter(cfg) as adapter:
            existing = await adapter.get_positions()
            if existing and not args.allow_existing_positions:
                labels = ", ".join(f"{row.symbol}:{row.side.value}:{row.qty:g}" for row in existing[:8])
                raise MexcWebError(f"LIVE account already has open Futures positions ({labels}); refusing to mix strategies")

            for symbol in symbols:
                detail = await adapter.get_contract_detail(symbol)
                contract_size = float(detail.get("contractSize") or 0)
                min_vol = float(detail.get("minVol") or 0)
                max_lev = int(detail.get("maxLeverage") or 1)
                max_vol = float(detail.get("maxVol") or math.inf)
                price_unit = float(detail.get("priceUnit") or 0)
                if contract_size > 0 and min_vol > 0 and max_lev > 0 and price_unit > 0:
                    contract_by_symbol[symbol] = (contract_size * min_vol, max_lev, contract_size * max_vol, price_unit)
            symbols = [symbol for symbol in symbols if symbol in contract_by_symbol]
            if not symbols:
                raise MexcWebError("No LIVE symbol has valid sizing metadata")

            console.print(
                f"[bold red]LIVE REAL-MONEY MICROSPREAD[/bold red]: {len(symbols)} zero-fee symbol(s); "
                "Binance + MEXC LIVE WebSocket signal, MEXC web-session execution."
            )
            console.print(
                f"Sizing: target_notional={args.target_notional_usdt:g}USDT, leverage cap={args.leverage}x "
                f"(contract maximum applied), IOC cross={args.ioc_cross_bps:g}bps"
            )
            console.print(f"Trade log: {log_path.resolve()}")

            try:
                while time.monotonic() < deadline and cycles < args.max_cycles:
                    now = time.monotonic()
                    now_ms = int(time.time() * 1000)

                    if position is None:
                        candidates: list[Candidate] = []
                        if now >= warmup_until:
                            for symbol in symbols:
                                if not fee_cache.fresh_zero(symbol, now_ms):
                                    continue
                                book = mexc.books.get(symbol)
                                if book is None:
                                    continue
                                book_age = now_ms - book.recv_ms
                                if book_age < 0 or book_age > args.max_book_age_ms:
                                    continue
                                if now_ms - last_entry_ms[symbol] < args.entry_cooldown_ms:
                                    continue
                                row = _candidate(symbol, models[symbol], book, args, now_ms, consume=False)
                                if row is not None:
                                    candidates.append(row)

                        best = _choose_best(candidates)
                        if best is not None:
                            fresh_now_ms = int(time.time() * 1000)
                            fresh_book = mexc.books.get(best.symbol)
                            fresh = (
                                _candidate(best.symbol, models[best.symbol], fresh_book, args, fresh_now_ms, consume=True)
                                if fresh_book is not None and fee_cache.fresh_zero(best.symbol, fresh_now_ms)
                                else None
                            )
                            if fresh is not None and fresh.direction == best.direction:
                                min_qty, max_lev, max_qty, price_unit = contract_by_symbol[fresh.symbol]
                                side = OrderSide.LONG if fresh.direction > 0 else OrderSide.SHORT
                                leverage = min(max(1, int(args.leverage)), max_lev)
                                limit_price = _marketable_ioc_price(side, fresh.book, args.ioc_cross_bps, price_unit)
                                requested_qty = min(max_qty, max(min_qty, args.target_notional_usdt / limit_price))
                                signal_ms = time.time_ns() / 1_000_000.0
                                fill = await adapter.open_ioc(
                                    symbol=fresh.symbol,
                                    side=side,
                                    price=limit_price,
                                    qty=requested_qty,
                                    leverage=leverage,
                                    client_order_id=f"live-entry-{uuid.uuid4().hex}"[:32],
                                )
                                result_ms = time.time_ns() / 1_000_000.0
                                if fill.filled_qty > 0:
                                    remote = await _resolve_remote_position(adapter, fresh.symbol, side, fill, leverage)
                                    position = LivePosition(
                                        remote=remote,
                                        direction=fresh.direction,
                                        entry_edge_bps=fresh.edge_bps,
                                        entry_spread_bps=fresh.spread_bps,
                                        opened_monotonic=time.monotonic(),
                                        entry_ms=int(time.time() * 1000),
                                        entry_fill=fill,
                                        trailing=PositiveTrailing(
                                            distance_bps=max(args.trailing_distance_bps, fresh.spread_bps)
                                        ),
                                    )
                                    last_entry_ms[fresh.symbol] = int(time.time() * 1000)
                                    console.print(
                                        f"[green]LIVE ENTRY[/green] {fresh.symbol} {'LONG' if fresh.direction > 0 else 'SHORT'} "
                                        f"qty={remote.qty:g} fill={remote.entry_price:g} lev={leverage}x "
                                        f"residual={fresh.edge_bps:+.3f}bps spread={fresh.spread_bps:.3f}bps "
                                        f"signal_to_ioc_result={result_ms-signal_ms:.1f}ms fee={fill.fee_usdt:g}"
                                    )
                                    if fill.fee_usdt != 0.0:
                                        await _emergency_close(adapter, position, "entry_fee_nonzero")
                                        position = None
                                        raise MexcWebError(f"LIVE zero-fee invariant violated on entry: fee={fill.fee_usdt:g}")

                    else:
                        symbol = position.remote.symbol
                        book = mexc.books.get(symbol)
                        reason: str | None = None
                        snap = models[symbol].snapshot(now_ms=now_ms, threshold_bps=0.0)
                        age_s = now - position.opened_monotonic

                        if not fee_cache.fresh_zero(symbol, now_ms):
                            reason = "fee_gate_lost"
                        elif book is None or now_ms - book.recv_ms > args.position_data_stale_ms:
                            reason = "mexc_book_stale"
                        elif snap.binance_age_ms > args.position_data_stale_ms:
                            reason = "binance_book_stale"
                        else:
                            executable_exit = book.bid if position.direction > 0 else book.ask
                            move_bps = _signed_move_bps(position.direction, position.remote.entry_price, executable_exit)
                            position.mfe_bps = max(position.mfe_bps, move_bps)
                            position.mae_bps = min(position.mae_bps, move_bps)
                            trail = position.trailing.update(move_bps)
                            if trail is not None and move_bps <= trail and age_s >= args.min_hold_seconds:
                                reason = "positive_trailing_stop"
                            elif move_bps <= -args.adverse_cut_bps and age_s >= args.min_hold_seconds:
                                reason = "adverse_cut"
                            elif (
                                abs(snap.edge_bps)
                                <= max(args.convergence_bps, abs(position.entry_edge_bps) * args.convergence_fraction)
                                and age_s >= args.min_hold_seconds
                            ):
                                reason = "convergence"
                            elif (
                                snap.direction == -position.direction
                                and abs(snap.edge_bps) >= args.reversal_edge_bps
                                and age_s >= args.min_hold_seconds
                            ):
                                reason = "residual_reversal"
                            elif age_s >= args.max_hold_seconds:
                                reason = "timeout"

                        if reason is not None:
                            exit_fill = await _close_position_fully(adapter, position.remote)
                            exit_ms = int(time.time() * 1000)
                            exit_price = float(exit_fill.avg_price or position.remote.entry_price)
                            pnl_bps = _signed_move_bps(position.direction, position.remote.entry_price, exit_price)
                            gross_pnl_usdt = (
                                position.direction * (exit_price - position.remote.entry_price) * position.remote.qty
                            )
                            pnl_usdt = gross_pnl_usdt - position.entry_fill.fee_usdt - exit_fill.fee_usdt
                            realized_pnl_bps += pnl_bps
                            realized_pnl_usdt += pnl_usdt
                            peak_realized_usdt = max(peak_realized_usdt, realized_pnl_usdt)
                            max_drawdown_usdt = max(max_drawdown_usdt, peak_realized_usdt - realized_pnl_usdt)
                            _append_trade(log_path, {
                                "entry_ms": position.entry_ms,
                                "exit_ms": exit_ms,
                                "symbol": symbol,
                                "direction": "LONG" if position.direction > 0 else "SHORT",
                                "qty": position.remote.qty,
                                "leverage": position.remote.leverage,
                                "entry_price": position.remote.entry_price,
                                "exit_price": exit_price,
                                "entry_edge_bps": position.entry_edge_bps,
                                "entry_spread_bps": position.entry_spread_bps,
                                "pnl_bps": pnl_bps,
                                "pnl_usdt": pnl_usdt,
                                "mfe_bps": position.mfe_bps,
                                "mae_bps": position.mae_bps,
                                "hold_ms": int(age_s * 1000),
                                "exit_reason": reason,
                                "entry_fee_usdt": position.entry_fill.fee_usdt,
                                "exit_fee_usdt": exit_fill.fee_usdt,
                                "entry_order_id": position.entry_fill.order_id,
                                "exit_order_id": exit_fill.order_id,
                            })
                            console.print(
                                f"[cyan]LIVE EXIT[/cyan] {symbol} reason={reason} pnl={pnl_bps:+.3f}bps/{pnl_usdt:+.6f}USDT "
                                f"MFE={position.mfe_bps:+.3f} MAE={position.mae_bps:+.3f} hold={age_s:.3f}s "
                                f"fees={position.entry_fill.fee_usdt + exit_fill.fee_usdt:g}"
                            )
                            position = None
                            cycles += 1
                            if realized_pnl_usdt <= -abs(args.max_session_loss_usdt):
                                raise MexcWebError(
                                    f"session loss kill-switch hit: {realized_pnl_usdt:.6f}USDT "
                                    f"<= -{abs(args.max_session_loss_usdt):.6f}USDT"
                                )

                    if now >= next_heartbeat:
                        fee_age = now_ms - fee_cache.checked_ms if fee_cache.checked_ms else math.inf
                        console.print(
                            f"LIVE HEARTBEAT state={'POSITION' if position else ('WARMUP' if now < warmup_until else 'WATCHING')} "
                            f"cycles={cycles}/{args.max_cycles} books={len(mexc.books)}/{len(symbols)} "
                            f"Bquotes={binance.quotes} Mdepth={mexc.updates} fee_age={fee_age:.0f}ms "
                            f"pnl={realized_pnl_usdt:+.6f}USDT ({realized_pnl_bps:+.3f}bps-sum) "
                            f"dd={max_drawdown_usdt:.6f}USDT"
                        )
                        next_heartbeat = now + args.heartbeat_seconds

                    wake.clear()
                    try:
                        await asyncio.wait_for(
                            wake.wait(),
                            timeout=0.10 if position is not None else args.heartbeat_seconds,
                        )
                    except TimeoutError:
                        pass
            except asyncio.CancelledError:
                if position is not None:
                    try:
                        await _emergency_close(adapter, position, "task_cancelled")
                        position = None
                    except Exception as close_exc:
                        console.print(f"[bold red]EMERGENCY CLOSE FAILED[/bold red]: {close_exc}")
                raise
            finally:
                if position is not None:
                    try:
                        await _emergency_close(adapter, position, "runner_shutdown")
                        position = None
                    except Exception as close_exc:
                        console.print(f"[bold red]SHUTDOWN CLOSE FAILED[/bold red]: {close_exc}")
                        raise
    finally:
        fee_stop.set()
        fee_task.cancel()
        try:
            await fee_task
        except asyncio.CancelledError:
            pass
        await binance.close()
        await mexc.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Real-money Binance->MEXC microspread runner using the existing MEXC web-session execution adapter."
    )
    parser.add_argument("--confirm-live", default="")
    parser.add_argument("--include-symbols", default="")
    parser.add_argument("--exclude-symbols", default="")
    parser.add_argument("--allow-existing-positions", action="store_true")
    parser.add_argument("--session-seconds", type=float, default=3600.0)
    parser.add_argument("--max-cycles", type=int, default=20)
    parser.add_argument("--warmup-seconds", type=float, default=8.0)
    parser.add_argument("--target-notional-usdt", type=float, default=10.0)
    parser.add_argument("--leverage", type=int, default=1000)
    parser.add_argument("--micro-horizon-ms", type=int, default=100)
    parser.add_argument("--baseline-seconds", type=float, default=8.0)
    parser.add_argument("--baseline-exclusion-ms", type=int, default=1000)
    parser.add_argument("--min-edge-bps", type=float, default=0.35)
    parser.add_argument("--min-net-edge-bps", type=float, default=0.20)
    parser.add_argument("--edge-to-spread-ratio", type=float, default=1.05)
    parser.add_argument("--min-binance-move-bps", type=float, default=0.02)
    parser.add_argument("--max-binance-age-ms", type=float, default=300.0)
    parser.add_argument("--max-mexc-age-ms", type=float, default=2000.0)
    parser.add_argument("--max-book-age-ms", type=float, default=1000.0)
    parser.add_argument("--position-data-stale-ms", type=float, default=3000.0)
    parser.add_argument("--rearm-fraction", type=float, default=0.35)
    parser.add_argument("--entry-cooldown-ms", type=int, default=250)
    parser.add_argument("--ioc-cross-bps", type=float, default=1.0)
    parser.add_argument("--min-hold-seconds", type=float, default=0.05)
    parser.add_argument("--max-hold-seconds", type=float, default=15.0)
    parser.add_argument("--convergence-bps", type=float, default=0.10)
    parser.add_argument("--convergence-fraction", type=float, default=0.20)
    parser.add_argument("--reversal-edge-bps", type=float, default=0.35)
    parser.add_argument("--adverse-cut-bps", type=float, default=1.5)
    parser.add_argument("--trailing-distance-bps", type=float, default=1.5)
    parser.add_argument("--max-session-loss-usdt", type=float, default=2.0)
    parser.add_argument("--heartbeat-seconds", type=float, default=5.0)
    parser.add_argument("--trade-csv", default="")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    if args.target_notional_usdt <= 0:
        raise SystemExit("--target-notional-usdt must be positive")
    if args.max_session_loss_usdt <= 0:
        raise SystemExit("--max-session-loss-usdt must be positive")
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        console.print("\n[yellow]LIVE stop requested.[/yellow]")
        raise SystemExit(130)
    except Exception as exc:
        console.print(f"[red]LIVE RUNNER STOPPED:[/red] {type(exc).__name__}: {exc}")
        raise SystemExit(2)


if __name__ == "__main__":
    main()
