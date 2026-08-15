from __future__ import annotations

import argparse
import asyncio
import csv
import math
import statistics
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path

import aiohttp
from dotenv import load_dotenv
from rich.console import Console

from .demo_microspread_test import (
    _adverse_cut_for_leverage,
    _required_edge,
    _signed_move_bps,
    _update_leverage_normalized_trailing,
)
from .live_lead_lag_shadow import PositiveTrailing
from .live_zero_fee_universe import LiveZeroFeeContract, discover_live_zero_fee_crosslisted
from .microspread import BinanceImpulseModel
from .microspread_feed import EventBinanceBookTickerFeed, EventMexcDepthFeed, LiveBook

console = Console()


@dataclass(frozen=True, slots=True)
class LatencySample:
    entry_ms: float
    exit_ms: float


class LiveRttProbe:
    """Continuously measure the current read-only network path to MEXC LIVE."""

    def __init__(self, *, symbol: str, interval_seconds: float, window: int = 60) -> None:
        self.symbol = symbol
        self.interval_seconds = max(0.10, float(interval_seconds))
        self.samples_ms: deque[float] = deque(maxlen=max(3, int(window)))
        self.last_sample_at = 0.0
        self.last_error: str | None = None
        self._stop = asyncio.Event()
        self._task: asyncio.Task | None = None

    @property
    def median_rtt_ms(self) -> float | None:
        return statistics.median(self.samples_ms) if self.samples_ms else None

    def current_one_way_ms(self, *, now: float, max_age_seconds: float) -> float | None:
        median = self.median_rtt_ms
        if median is None or now - self.last_sample_at > max(0.1, max_age_seconds):
            return None
        return median / 2.0

    async def start(self) -> None:
        self._stop.clear()
        self._task = asyncio.create_task(self._run())

    async def close(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _run(self) -> None:
        timeout = aiohttp.ClientTimeout(total=5.0)
        url = "https://contract.mexc.com/api/v1/contract/ticker"
        async with aiohttp.ClientSession(timeout=timeout) as session:
            while not self._stop.is_set():
                started = time.perf_counter()
                try:
                    async with session.get(url, params={"symbol": self.symbol}) as response:
                        response.raise_for_status()
                        await response.read()
                    self.samples_ms.append((time.perf_counter() - started) * 1000.0)
                    self.last_sample_at = time.monotonic()
                    self.last_error = None
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self.last_error = f"{type(exc).__name__}: {exc}"
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=self.interval_seconds)
                except TimeoutError:
                    pass


@dataclass(frozen=True, slots=True)
class PendingEntry:
    symbol: str
    direction: int
    signal_ms: int
    execute_at: float
    impulse_bps: float
    signal_spread_bps: float
    target_price: float
    exit_latency_ms: float
    requested_notional_usdt: float


@dataclass(slots=True)
class ShadowPosition:
    symbol: str
    direction: int
    entry_price: float
    entry_ms: int
    signal_ms: int
    impulse_bps: float
    entry_spread_bps: float
    target_price: float
    exit_latency_ms: float
    trailing: PositiveTrailing
    requested_notional_usdt: float
    filled_notional_usdt: float
    base_qty: float
    fill_ratio: float
    entry_limit_price: float
    entry_depth_levels: int
    entry_book_levels: int
    entry_visible_notional_usdt: float
    mfe_bps: float = 0.0
    mae_bps: float = 0.0


@dataclass(frozen=True, slots=True)
class PendingExit:
    reason: str
    decision_ms: int
    execute_at: float


@dataclass(frozen=True, slots=True)
class ShadowTrade:
    symbol: str
    direction: int
    signal_ms: int
    entry_ms: int
    exit_ms: int
    entry_price: float
    exit_price: float
    impulse_bps: float
    entry_spread_bps: float
    pnl_bps: float
    pnl_usdt: float
    mfe_bps: float
    mae_bps: float
    hold_ms: int
    signal_to_fill_ms: int
    exit_decision_to_fill_ms: int
    exit_reason: str
    requested_notional_usdt: float = 0.0
    filled_notional_usdt: float = 0.0
    fill_ratio: float = 1.0
    entry_base_qty: float = 0.0
    entry_limit_price: float = 0.0
    entry_depth_levels: int = 0
    exit_depth_levels: int = 0
    entry_book_levels: int = 0
    entry_visible_notional_usdt: float = 0.0
    equity_before_usdt: float = 0.0
    equity_after_usdt: float = 0.0


@dataclass(frozen=True, slots=True)
class DepthFill:
    requested_base_qty: float
    filled_base_qty: float
    avg_price: float
    filled_notional_usdt: float
    levels_used: int
    available_base_qty: float
    available_notional_usdt: float

    @property
    def fill_ratio(self) -> float:
        if self.requested_base_qty <= 0:
            return 0.0
        return min(1.0, self.filled_base_qty / self.requested_base_qty)


def _walk_depth(
    levels: tuple[tuple[float, float], ...],
    *,
    requested_base_qty: float,
    contract_size: float,
    buy: bool,
    limit_price: float | None,
    slippage_bps: float,
) -> DepthFill:
    """Walk one immutable MEXC depth snapshot without inventing liquidity.

    MEXC depth volume is contract count, so each level is converted through
    ``contract_size`` before it contributes to the simulated IOC fill.
    """
    requested = max(0.0, float(requested_base_qty))
    size = max(0.0, float(contract_size))
    if requested <= 0 or size <= 0:
        return DepthFill(requested, 0.0, 0.0, 0.0, 0, 0.0, 0.0)
    slip = max(0.0, float(slippage_bps)) / 10_000.0
    remaining = requested
    filled = cost = 0.0
    available_base = available_notional = 0.0
    used = 0
    for raw_price, contracts in levels:
        price = float(raw_price)
        if limit_price is not None and ((buy and price > limit_price) or (not buy and price < limit_price)):
            break
        available = max(0.0, float(contracts)) * size
        execution_price = price * (1.0 + slip if buy else 1.0 - slip)
        available_base += available
        available_notional += available * execution_price
        take = min(remaining, available)
        if take <= 0:
            continue
        filled += take
        cost += take * execution_price
        remaining -= take
        used += 1
    avg = cost / filled if filled > 0 else 0.0
    return DepthFill(
        requested, filled, avg, cost, used, available_base, available_notional,
    )


def _simulate_ioc_entry(
    book: LiveBook,
    *,
    direction: int,
    requested_notional_usdt: float,
    contract_size: float,
    limit_offset_bps: float,
    slippage_bps: float,
) -> tuple[DepthFill, float]:
    best = book.ask if direction > 0 else book.bid
    offset = max(0.0, float(limit_offset_bps)) / 10_000.0
    limit_price = best * (1.0 + offset if direction > 0 else 1.0 - offset)
    requested_base = max(0.0, requested_notional_usdt) / limit_price if limit_price > 0 else 0.0
    levels = book.asks if direction > 0 else book.bids
    return _walk_depth(
        levels,
        requested_base_qty=requested_base,
        contract_size=contract_size,
        buy=direction > 0,
        limit_price=limit_price,
        slippage_bps=slippage_bps,
    ), limit_price


def _simulate_market_exit(
    book: LiveBook,
    *,
    position_direction: int,
    base_qty: float,
    contract_size: float,
    slippage_bps: float,
) -> DepthFill:
    buy = position_direction < 0
    return _walk_depth(
        book.asks if buy else book.bids,
        requested_base_qty=base_qty,
        contract_size=contract_size,
        buy=buy,
        limit_price=None,
        slippage_bps=slippage_bps,
    )


def _scaled_requested_notional(
    *,
    base_notional_usdt: float,
    equity_usdt: float,
    initial_equity_usdt: float,
    leverage: int,
    max_margin_fraction: float,
    max_notional_usdt: float,
    enabled: bool,
) -> float:
    base = max(0.0, float(base_notional_usdt))
    equity = max(0.0, float(equity_usdt))
    if not enabled:
        return base
    initial = max(0.01, float(initial_equity_usdt))
    compounded = base * equity / initial
    affordable = equity * max(1, int(leverage)) * min(1.0, max(0.0, float(max_margin_fraction)))
    requested = min(compounded, affordable)
    if max_notional_usdt > 0:
        requested = min(requested, float(max_notional_usdt))
    return max(0.0, requested)


def _entry_price(book: LiveBook, direction: int, slippage_bps: float) -> float:
    slip = max(0.0, slippage_bps) / 10_000.0
    return book.ask * (1.0 + slip) if direction > 0 else book.bid * (1.0 - slip)


def _exit_price(book: LiveBook, direction: int, slippage_bps: float) -> float:
    slip = max(0.0, slippage_bps) / 10_000.0
    return book.bid * (1.0 - slip) if direction > 0 else book.ask * (1.0 + slip)


def _fresh(book: LiveBook | None, now_ms: int, max_age_ms: float) -> bool:
    return book is not None and 0 <= now_ms - book.recv_ms <= max_age_ms


def _write_csv(path: Path, trades: list[ShadowTrade]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(ShadowTrade.__dataclass_fields__))
        writer.writeheader()
        for trade in trades:
            row = {name: getattr(trade, name) for name in ShadowTrade.__dataclass_fields__}
            row["direction"] = "LONG" if trade.direction > 0 else "SHORT"
            writer.writerow(row)


def _load_latency_samples(path: Path) -> list[LatencySample]:
    samples: list[LatencySample] = []
    with path.open("r", newline="", encoding="utf-8-sig") as handle:
        for row in csv.DictReader(handle):
            if row.get("event") != "demo_exit":
                continue
            try:
                provisional = row.get("signal_to_provisional_ms") or ""
                entry_ms = (
                    float(provisional)
                    if provisional
                    else float(row.get("signal_to_ioc_post_ms") or "")
                    + float(row.get("ioc_confirmation_ms") or "")
                )
                exit_ms = float(row.get("ioc_post_roundtrip_ms") or "")
            except ValueError:
                continue
            if entry_ms > 0 and exit_ms > 0 and math.isfinite(entry_ms) and math.isfinite(exit_ms):
                samples.append(LatencySample(entry_ms=entry_ms, exit_ms=exit_ms))
    if not samples:
        raise ValueError(f"no usable Demo latency rows in {path}")
    return samples


def _summary(trades: list[ShadowTrade]) -> dict[str, float | int]:
    wins = [row.pnl_usdt for row in trades if row.pnl_usdt > 0]
    losses = [-row.pnl_usdt for row in trades if row.pnl_usdt < 0]
    flats = sum(row.pnl_usdt == 0 for row in trades)
    gross_profit = sum(wins)
    gross_loss = sum(losses)
    return {
        "trades": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "flats": flats,
        "pnl_usdt": sum(row.pnl_usdt for row in trades),
        "win_rate": len(wins) / max(1, len(wins) + len(losses)) * 100.0,
        "profit_factor": gross_profit / gross_loss if gross_loss else (math.inf if gross_profit else 0.0),
    }


async def _yield_market_update(wake: asyncio.Event, timeout_seconds: float) -> None:
    if not wake.is_set():
        try:
            await asyncio.wait_for(wake.wait(), timeout=timeout_seconds)
        except TimeoutError:
            pass
    wake.clear()


async def run(args: argparse.Namespace) -> list[ShadowTrade]:
    root = Path(__file__).resolve().parents[2]
    load_dotenv(root / ".env", override=False)
    requested = {item.strip().upper() for item in args.symbols.split(",") if item.strip()}
    if args.live_latency_probe and args.latency_csv:
        raise ValueError("--live-latency-probe and --latency-csv are mutually exclusive")
    latency_samples = _load_latency_samples(Path(args.latency_csv)) if args.latency_csv else []
    universe = await discover_live_zero_fee_crosslisted()
    contracts_by_symbol = {row.mexc_symbol: row for row in universe if row.mexc_symbol in requested}
    missing = sorted(requested - set(contracts_by_symbol))
    if missing:
        raise RuntimeError(f"symbols are not currently LIVE exact 0/0 and Binance-crosslisted: {', '.join(missing)}")
    contracts: list[LiveZeroFeeContract] = [contracts_by_symbol[symbol] for symbol in sorted(requested)]

    wake = asyncio.Event()
    models = {
        row.mexc_symbol: BinanceImpulseModel(
            horizon_ms=args.horizon_ms,
            min_edge_bps=args.min_edge_bps,
            max_binance_age_ms=args.max_binance_age_ms,
            rearm_fraction=args.rearm_fraction,
        )
        for row in contracts
    }
    binance = EventBinanceBookTickerFeed(contracts, models, wake)
    mexc = EventMexcDepthFeed(
        [row.mexc_symbol for row in contracts], models, wake, depth_limit=args.depth_levels,
    )
    latency_probe = (
        LiveRttProbe(
            symbol=(args.latency_probe_symbol or contracts[0].mexc_symbol),
            interval_seconds=args.latency_probe_interval_seconds,
        )
        if args.live_latency_probe
        else None
    )
    if latency_probe is not None:
        await latency_probe.start()
    await binance.start()
    await mexc.start()

    output = Path(args.output) if args.output else root / f"binance_impulse_live_shadow_{int(time.time())}.csv"
    trades: list[ShadowTrade] = []
    position: ShadowPosition | None = None
    pending_entry: PendingEntry | None = None
    pending_exit: PendingExit | None = None
    last_entry_ms = -10**18
    started = time.monotonic()
    deadline = started + args.seconds
    warmup_until = started + args.warmup_seconds
    next_status = started
    next_fee_refresh = started + args.fee_refresh_seconds
    eligible = set(requested)
    cumulative_pnl = 0.0
    virtual_equity = max(0.01, float(args.initial_equity_usdt))
    latency_index = 0

    console.print(
        "[cyan]LIVE BINANCE IMPULSE SHADOW ONLY[/cyan]: public Binance/MEXC WebSockets and "
        "read-only MEXC fee discovery; no execution adapter and no order endpoint."
    )
    console.print(
        f"symbols={','.join(sorted(requested))} notional={args.notional_usdt:g}USDT "
        f"depth={'ON:'+str(args.depth_levels) if args.depth_aware else 'OFF'} "
        f"scaling={'equity:'+format(virtual_equity, 'g') if args.scale_notional_with_equity else 'fixed'} "
        f"latency={'LiveRTT' if latency_probe else 'DemoReplay:'+str(len(latency_samples)) if latency_samples else f'fixed:{args.entry_latency_ms:g}/{args.exit_latency_ms:g}ms'} "
        f"slippage_each_side={args.slippage_bps:g}bps"
    )

    try:
        while (
            (time.monotonic() < deadline and len(trades) < args.max_trades and cumulative_pnl > -abs(args.max_loss_usdt))
            or position is not None
            or pending_exit is not None
        ):
            now = time.monotonic()
            now_ms = int(time.time() * 1000)

            if now >= next_fee_refresh and position is None and pending_entry is None:
                refreshed = await discover_live_zero_fee_crosslisted()
                eligible = {row.mexc_symbol for row in refreshed if row.mexc_symbol in requested}
                next_fee_refresh = now + args.fee_refresh_seconds

            if pending_exit is not None and position is not None and now >= pending_exit.execute_at:
                book = mexc.books.get(position.symbol)
                if _fresh(book, now_ms, args.max_book_age_ms):
                    assert book is not None
                    contract = contracts_by_symbol[position.symbol]
                    exit_levels = 0
                    if args.depth_aware:
                        depth_fill = _simulate_market_exit(
                            book,
                            position_direction=position.direction,
                            base_qty=position.base_qty,
                            contract_size=contract.contract_size,
                            slippage_bps=args.slippage_bps,
                        )
                        if depth_fill.fill_ratio < 1.0 - 1e-9:
                            await _yield_market_update(wake, args.idle_timeout_seconds)
                            continue
                        price = depth_fill.avg_price
                        exit_levels = depth_fill.levels_used
                    else:
                        price = _exit_price(book, position.direction, args.slippage_bps)
                    pnl_bps = _signed_move_bps(position.direction, position.entry_price, price)
                    pnl_usdt = position.direction * position.base_qty * (price - position.entry_price)
                    equity_before = virtual_equity
                    virtual_equity = max(0.0, virtual_equity + pnl_usdt)
                    trade = ShadowTrade(
                        symbol=position.symbol,
                        direction=position.direction,
                        signal_ms=position.signal_ms,
                        entry_ms=position.entry_ms,
                        exit_ms=now_ms,
                        entry_price=position.entry_price,
                        exit_price=price,
                        impulse_bps=position.impulse_bps,
                        entry_spread_bps=position.entry_spread_bps,
                        pnl_bps=pnl_bps,
                        pnl_usdt=pnl_usdt,
                        mfe_bps=position.mfe_bps,
                        mae_bps=position.mae_bps,
                        hold_ms=now_ms - position.entry_ms,
                        signal_to_fill_ms=position.entry_ms - position.signal_ms,
                        exit_decision_to_fill_ms=now_ms - pending_exit.decision_ms,
                        exit_reason=pending_exit.reason,
                        requested_notional_usdt=position.requested_notional_usdt,
                        filled_notional_usdt=position.filled_notional_usdt,
                        fill_ratio=position.fill_ratio,
                        entry_base_qty=position.base_qty,
                        entry_limit_price=position.entry_limit_price,
                        entry_depth_levels=position.entry_depth_levels,
                        exit_depth_levels=exit_levels,
                        entry_book_levels=position.entry_book_levels,
                        entry_visible_notional_usdt=position.entry_visible_notional_usdt,
                        equity_before_usdt=equity_before,
                        equity_after_usdt=virtual_equity,
                    )
                    trades.append(trade)
                    cumulative_pnl += pnl_usdt
                    console.print(
                        f"SHADOW EXIT {trade.symbol} {'LONG' if trade.direction > 0 else 'SHORT'} "
                        f"reason={trade.exit_reason} pnl={trade.pnl_bps:+.3f}bps/{trade.pnl_usdt:+.4f}USDT "
                        f"hold={trade.hold_ms}ms fill={trade.filled_notional_usdt:.2f}/"
                        f"{trade.requested_notional_usdt:.2f}USDT ({trade.fill_ratio:.1%}) "
                        f"equity={trade.equity_after_usdt:.4f}USDT"
                    )
                    _write_csv(output, trades)
                    position = None
                    pending_exit = None
                    last_entry_ms = now_ms
                await _yield_market_update(wake, args.idle_timeout_seconds)
                continue

            if position is not None:
                book = mexc.books.get(position.symbol)
                if _fresh(book, now_ms, args.max_book_age_ms):
                    assert book is not None
                    mark = book.bid if position.direction > 0 else book.ask
                    move_bps = _signed_move_bps(position.direction, position.entry_price, mark)
                    position.mfe_bps = max(position.mfe_bps, move_bps)
                    position.mae_bps = min(position.mae_bps, move_bps)
                    trail = _update_leverage_normalized_trailing(
                        position.trailing, move_bps, leverage=args.leverage,
                    )
                    age_s = (now_ms - position.entry_ms) / 1000.0
                    adverse = _adverse_cut_for_leverage(
                        leverage=args.leverage,
                        spread_bps=position.entry_spread_bps,
                        fixed_cut_bps=args.adverse_cut_bps,
                        spread_multiple=args.adverse_spread_mult,
                        adverse_roe_pct=0.0,
                    )
                    reason = None
                    if trail is not None and move_bps <= trail and age_s >= args.min_hold_seconds:
                        reason = "positive_trailing_stop"
                    if reason is None and move_bps <= -adverse and age_s >= args.min_hold_seconds:
                        reason = "live_adverse_cut"
                    target_caught = mark >= position.target_price if position.direction > 0 else mark <= position.target_price
                    if reason is None and target_caught and move_bps >= args.min_exit_profit_bps and age_s >= args.min_hold_seconds:
                        reason = "binance_target_caught"
                    snap = models[position.symbol].snapshot(now_ms=now_ms, threshold_bps=0.0)
                    if (
                        reason is None
                        and snap.binance_move_bps * position.direction <= -args.reversal_edge_bps
                        and age_s >= args.min_hold_seconds
                    ):
                        reason = "binance_impulse_reversed"
                    if reason is None and age_s >= args.max_hold_seconds:
                        reason = "timeout"
                    if reason is None and now >= deadline:
                        reason = "session_end"
                    if reason is not None:
                        current_one_way_ms = (
                            latency_probe.current_one_way_ms(
                                now=now, max_age_seconds=args.latency_probe_max_age_seconds,
                            )
                            if latency_probe is not None
                            else None
                        )
                        if latency_probe is not None and current_one_way_ms is None:
                            await _yield_market_update(wake, args.idle_timeout_seconds)
                            continue
                        exit_latency_ms = (
                            args.order_build_ms + current_one_way_ms
                            if current_one_way_ms is not None
                            else position.exit_latency_ms
                        )
                        pending_exit = PendingExit(
                            reason=reason,
                            decision_ms=now_ms,
                            execute_at=now + exit_latency_ms / 1000.0,
                        )
                await _yield_market_update(wake, args.idle_timeout_seconds)
                continue

            if pending_entry is not None:
                if now >= deadline or pending_entry.symbol not in eligible:
                    pending_entry = None
                elif now >= pending_entry.execute_at:
                    book = mexc.books.get(pending_entry.symbol)
                    if _fresh(book, now_ms, args.max_book_age_ms):
                        assert book is not None
                        contract = contracts_by_symbol[pending_entry.symbol]
                        leverage = min(max(1, int(args.leverage)), max(1, contract.max_leverage))
                        if args.depth_aware:
                            fill, entry_limit = _simulate_ioc_entry(
                                book,
                                direction=pending_entry.direction,
                                requested_notional_usdt=pending_entry.requested_notional_usdt,
                                contract_size=contract.contract_size,
                                limit_offset_bps=args.ioc_limit_offset_bps,
                                slippage_bps=args.slippage_bps,
                            )
                            if fill.filled_base_qty + 1e-12 < contract.contract_size * contract.min_vol:
                                console.print(
                                    f"SHADOW IOC UNFILLED {pending_entry.symbol} requested="
                                    f"{pending_entry.requested_notional_usdt:.2f}USDT"
                                )
                                pending_entry = None
                                await _yield_market_update(wake, args.idle_timeout_seconds)
                                continue
                            price = fill.avg_price
                            base_qty = fill.filled_base_qty
                            filled_notional = fill.filled_notional_usdt
                            fill_ratio = fill.fill_ratio
                            entry_levels = fill.levels_used
                            entry_book_levels = len(book.asks if pending_entry.direction > 0 else book.bids)
                            entry_visible_notional = fill.available_notional_usdt
                        else:
                            price = _entry_price(book, pending_entry.direction, args.slippage_bps)
                            base_qty = pending_entry.requested_notional_usdt / price
                            filled_notional = pending_entry.requested_notional_usdt
                            fill_ratio = 1.0
                            entry_limit = price
                            entry_levels = 0
                            entry_book_levels = 0
                            entry_visible_notional = 0.0
                        position = ShadowPosition(
                            symbol=pending_entry.symbol,
                            direction=pending_entry.direction,
                            entry_price=price,
                            entry_ms=now_ms,
                            signal_ms=pending_entry.signal_ms,
                            impulse_bps=pending_entry.impulse_bps,
                            entry_spread_bps=book.spread_bps,
                            target_price=pending_entry.target_price,
                            exit_latency_ms=pending_entry.exit_latency_ms,
                            trailing=PositiveTrailing(distance_bps=max(args.trailing_distance_bps, book.spread_bps)),
                            requested_notional_usdt=pending_entry.requested_notional_usdt,
                            filled_notional_usdt=filled_notional,
                            base_qty=base_qty,
                            fill_ratio=fill_ratio,
                            entry_limit_price=entry_limit,
                            entry_depth_levels=entry_levels,
                            entry_book_levels=entry_book_levels,
                            entry_visible_notional_usdt=entry_visible_notional,
                        )
                        console.print(
                            f"SHADOW ENTRY {position.symbol} {'LONG' if position.direction > 0 else 'SHORT'} "
                            f"impulse={position.impulse_bps:+.3f}bps spread={book.spread_bps:.3f} "
                            f"signal_to_fill={now_ms-position.signal_ms}ms px={price:g} "
                            f"fill={filled_notional:.2f}/{pending_entry.requested_notional_usdt:.2f}USDT "
                            f"({fill_ratio:.1%}) levels={entry_levels}/{entry_book_levels} "
                            f"visible={entry_visible_notional:.2f}USDT lev={leverage}x"
                        )
                        pending_entry = None
                await _yield_market_update(wake, args.idle_timeout_seconds)
                continue

            if now < warmup_until or now >= deadline or len(trades) >= args.max_trades:
                pass
            elif now_ms - last_entry_ms >= args.entry_cooldown_ms:
                for symbol in sorted(eligible):
                    book = mexc.books.get(symbol)
                    if not _fresh(book, now_ms, args.max_book_age_ms):
                        continue
                    assert book is not None
                    threshold = _required_edge(
                        spread_bps=book.spread_bps,
                        min_edge_bps=args.min_edge_bps,
                        min_net_edge_bps=args.min_net_edge_bps,
                        spread_ratio=args.edge_to_spread_ratio,
                    )
                    current_one_way_ms = (
                        latency_probe.current_one_way_ms(
                            now=now, max_age_seconds=args.latency_probe_max_age_seconds,
                        )
                        if latency_probe is not None
                        else None
                    )
                    if latency_probe is not None and current_one_way_ms is None:
                        continue
                    snap = models[symbol].signal(now_ms=now_ms, threshold_bps=threshold)
                    if not snap.ready:
                        continue
                    latency = (
                        LatencySample(args.order_build_ms + current_one_way_ms, 0.0)
                        if current_one_way_ms is not None
                        else latency_samples[latency_index % len(latency_samples)]
                        if latency_samples
                        else LatencySample(args.entry_latency_ms, args.exit_latency_ms)
                    )
                    latency_index += 1
                    contract = contracts_by_symbol[symbol]
                    leverage = min(max(1, int(args.leverage)), max(1, contract.max_leverage))
                    requested_notional = _scaled_requested_notional(
                        base_notional_usdt=args.notional_usdt,
                        equity_usdt=virtual_equity,
                        initial_equity_usdt=args.initial_equity_usdt,
                        leverage=leverage,
                        max_margin_fraction=args.max_margin_fraction,
                        max_notional_usdt=args.max_notional_usdt,
                        enabled=args.scale_notional_with_equity,
                    )
                    if requested_notional <= 0:
                        continue
                    pending_entry = PendingEntry(
                        symbol=symbol,
                        direction=snap.direction,
                        signal_ms=now_ms,
                        execute_at=now + latency.entry_ms / 1000.0,
                        impulse_bps=snap.binance_move_bps,
                        signal_spread_bps=book.spread_bps,
                        target_price=book.mid * math.exp(snap.binance_move_bps / 10_000.0),
                        exit_latency_ms=latency.exit_ms,
                        requested_notional_usdt=requested_notional,
                    )
                    console.print(
                        f"SHADOW SIGNAL {symbol} {'LONG' if snap.direction > 0 else 'SHORT'} "
                        f"impulse={snap.binance_move_bps:+.3f}bps threshold={threshold:.3f} "
                        f"spread={book.spread_bps:.3f} requested={requested_notional:.2f}USDT"
                    )
                    break

            if now >= next_status:
                stats = _summary(trades)
                state = (
                    "EXIT_PENDING" if pending_exit else "OPEN" if position
                    else "ENTRY_PENDING" if pending_entry else "WATCHING"
                )
                latency_status = ""
                if latency_probe is not None:
                    current_rtt = latency_probe.median_rtt_ms
                    latency_status = (
                        f"live_rtt={current_rtt:.1f}ms "
                        if current_rtt is not None
                        else f"live_rtt=PENDING error={latency_probe.last_error or '-'} "
                    )
                console.print(
                    f"SHADOW STATUS trades={stats['trades']} W/L/F={stats['wins']}/{stats['losses']}/{stats['flats']} "
                    f"pnl={stats['pnl_usdt']:+.4f}USDT books={len(mexc.books)}/{len(contracts)} "
                    f"Bquotes={binance.quotes} Mdepth={mexc.updates} "
                    f"{latency_status}equity={virtual_equity:.4f}USDT state={state}"
                )
                next_status = now + args.status_seconds

            await _yield_market_update(wake, args.idle_timeout_seconds)
    finally:
        await binance.close()
        await mexc.close()
        if latency_probe is not None:
            await latency_probe.close()
        _write_csv(output, trades)

    stats = _summary(trades)
    console.print(
        f"SHADOW COMPLETE trades={stats['trades']} W/L/F={stats['wins']}/{stats['losses']}/{stats['flats']} "
        f"winrate={stats['win_rate']:.2f}% PF={stats['profit_factor']:.3f} "
        f"ZERO_FEE_PNL={stats['pnl_usdt']:+.6f}USDT CSV={output.resolve()}"
    )
    console.print("READ-ONLY CONFIRMED: no LIVE or Demo order adapter was constructed.")
    return trades


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Read-only LIVE shadow for the frozen Binance impulse strategy")
    parser.add_argument("--symbols", default="XRP_USDT,LINK_USDT,DOGE_USDT")
    parser.add_argument("--seconds", type=float, default=21600.0)
    parser.add_argument("--max-trades", type=int, default=100)
    parser.add_argument("--notional-usdt", type=float, default=10_000.0)
    parser.add_argument(
        "--depth-aware", action="store_true",
        help="walk captured MEXC depth for IOC entry and reduce-only exit instead of assuming full top-book fills",
    )
    parser.add_argument("--depth-levels", type=int, default=20)
    parser.add_argument("--ioc-limit-offset-bps", type=float, default=5.0)
    parser.add_argument(
        "--scale-notional-with-equity", action="store_true",
        help="compound requested IOC notional from the virtual equity curve",
    )
    parser.add_argument("--initial-equity-usdt", type=float, default=60.0)
    parser.add_argument("--max-margin-fraction", type=float, default=0.90)
    parser.add_argument(
        "--max-notional-usdt", type=float, default=0.0,
        help="optional cap for compounded requested IOC notional; 0 means only the margin cap applies",
    )
    parser.add_argument("--entry-latency-ms", type=float, default=650.0)
    parser.add_argument("--exit-latency-ms", type=float, default=350.0)
    parser.add_argument("--live-latency-probe", action="store_true")
    parser.add_argument("--latency-probe-symbol", default="LINK_USDT")
    parser.add_argument("--latency-probe-interval-seconds", type=float, default=1.0)
    parser.add_argument("--latency-probe-max-age-seconds", type=float, default=3.0)
    parser.add_argument(
        "--order-build-ms",
        type=float,
        default=15.5,
        help="measured local signal-to-IOC-POST preparation time added to current LIVE half-RTT",
    )
    parser.add_argument(
        "--latency-csv",
        default="",
        help="replay measured signal-to-fill and IOC roundtrip rows from Demo excursion telemetry",
    )
    parser.add_argument("--slippage-bps", type=float, default=0.5)
    parser.add_argument("--warmup-seconds", type=float, default=3.0)
    parser.add_argument("--horizon-ms", type=int, default=100)
    parser.add_argument("--min-edge-bps", type=float, default=1.0)
    parser.add_argument("--min-net-edge-bps", type=float, default=0.2)
    parser.add_argument("--edge-to-spread-ratio", type=float, default=1.05)
    parser.add_argument("--max-binance-age-ms", type=float, default=300.0)
    parser.add_argument("--max-book-age-ms", type=float, default=750.0)
    parser.add_argument("--rearm-fraction", type=float, default=0.35)
    parser.add_argument("--entry-cooldown-ms", type=int, default=250)
    parser.add_argument("--min-hold-seconds", type=float, default=0.05)
    parser.add_argument("--max-hold-seconds", type=float, default=60.0)
    parser.add_argument("--adverse-cut-bps", type=float, default=1.5)
    parser.add_argument("--adverse-spread-mult", type=float, default=1.25)
    parser.add_argument("--min-exit-profit-bps", type=float, default=0.5)
    parser.add_argument("--reversal-edge-bps", type=float, default=1000.0)
    parser.add_argument("--trailing-distance-bps", type=float, default=1.0)
    parser.add_argument("--leverage", type=int, default=200)
    parser.add_argument("--max-loss-usdt", type=float, default=50.0)
    parser.add_argument("--fee-refresh-seconds", type=float, default=60.0)
    parser.add_argument("--status-seconds", type=float, default=5.0)
    parser.add_argument("--idle-timeout-seconds", type=float, default=0.05)
    parser.add_argument("--output", default="")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        console.print("[yellow]Read-only shadow stopped by user.[/yellow]")
    except Exception as exc:
        console.print(f"[red]LIVE SHADOW FAILED:[/red] {type(exc).__name__}: {exc}")
        raise SystemExit(2) from exc


if __name__ == "__main__":
    main()
