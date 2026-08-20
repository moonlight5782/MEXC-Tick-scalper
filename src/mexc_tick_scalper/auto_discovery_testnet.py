from __future__ import annotations

import asyncio
import csv
import math
import os
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from rich.console import Console

from . import auto_discovery_shadow as auto
from .execution import OrderFill, OrderSide, PositionSnapshot
from .lead_lag_strategy import LeadLagGate
from .live_lead_lag_shadow import PositiveTrailing
from .live_production_runner import _close_position_fully, _marketable_ioc_price, _resolve_remote_position, _signed_move_bps
from .microspread import MicroSpreadModel
from .microspread_feed import EventBinanceBookTickerFeed, EventMexcDepthFeed
from .prelive_latency_diagnostic import _exit_depth_for_qty
from .prelive_persistent_catchup_shadow import Signal, directional_move_bps
from .prelive_persistent_ioc_shadow import immediate_roundtrip_cost_bps, virtual_ioc_fill
from .prelive_persistent_ioc_shadow_v2 import _event_key, _valid_snapshot, entry_slippage_bps, executable_edge_ok
from .web_execution import DEMO_HOST, MexcWebError, MexcWebExecutionAdapter, WebExecutionConfig

console = Console()


@dataclass(slots=True)
class DemoPosition:
    signal: Signal
    remote: PositionSnapshot
    entry_fill: OrderFill
    entry_ms: int
    signal_ms: int
    live_entry_mid: float
    live_entry_binance: float
    live_entry_residual_bps: float
    actual_entry_price: float
    requested_notional_usdt: float
    filled_notional_usdt: float
    trailing: PositiveTrailing
    runner_armed: bool = False
    mfe_bps: float = 0.0
    mae_bps: float = 0.0
    last_progress_ms: int = 0
    last_position_poll_ms: int = 0


@dataclass(slots=True)
class Stats:
    signals: int = 0
    entries: int = 0
    expired: int = 0
    nofill: int = 0
    wins: int = 0
    losses: int = 0
    flats: int = 0
    pnl_usdt: float = 0.0
    gross_win_usdt: float = 0.0
    gross_loss_usdt: float = 0.0
    fills: list[float] = field(default_factory=list)
    notionals: list[float] = field(default_factory=list)
    holds: list[float] = field(default_factory=list)
    signal_to_fill: list[float] = field(default_factory=list)
    exits: dict[str, int] = field(default_factory=dict)

    @property
    def pf(self) -> float:
        if self.gross_loss_usdt <= 0:
            return math.inf if self.gross_win_usdt > 0 else 0.0
        return self.gross_win_usdt / self.gross_loss_usdt


@dataclass(slots=True)
class Bank:
    balance_usdt: float = auto.START_BANK_USDT

    @property
    def stop_balance(self) -> float:
        return auto.START_BANK_USDT * (1.0 - auto.MAX_SESSION_DRAWDOWN_FRACTION)

    @property
    def max_margin(self) -> float:
        return max(0.0, self.balance_usdt) * (1.0 - auto.MIN_EQUITY_RESERVE_FRACTION)


BANK = Bank()


def _assert_demo_write_config(cfg: WebExecutionConfig) -> None:
    cfg.validate_environment()
    if cfg.environment != "demo":
        raise MexcWebError(f"Testnet runner requires environment=demo, got {cfg.environment!r}")
    if not cfg.write_enabled:
        raise MexcWebError("Testnet runner requires demo writes enabled")
    if os.getenv("MEXC_DEMO_WRITE", "").strip().upper() != "YES":
        raise MexcWebError("Demo writes are locked. Set MEXC_DEMO_WRITE=YES in local .env to run Testnet orders.")
    if DEMO_HOST not in cfg.base_url:
        raise MexcWebError("Testnet runner refuses any non-demo execution host")


def _requested_notional(bank: Bank, leverage: float) -> tuple[float, float, float]:
    leverage = max(1.0, float(leverage))
    required_margin = auto.LEGACY_TARGET_NOTIONAL_USDT / leverage
    margin = min(required_margin, bank.max_margin)
    requested = margin * leverage
    reserve = max(0.0, bank.balance_usdt - margin)
    return requested, margin, reserve


def _summary(stats: Stats) -> str:
    closed = stats.wins + stats.losses + stats.flats
    wr = stats.wins / closed * 100.0 if closed else 0.0
    pf = "inf" if math.isinf(stats.pf) else f"{stats.pf:.3f}"
    fill_med = sorted(stats.fills)[len(stats.fills)//2] * 100.0 if stats.fills else 0.0
    notional_med = sorted(stats.notionals)[len(stats.notionals)//2] if stats.notionals else 0.0
    hold_med = sorted(stats.holds)[len(stats.holds)//2] if stats.holds else 0.0
    signal_fill_med = sorted(stats.signal_to_fill)[len(stats.signal_to_fill)//2] if stats.signal_to_fill else 0.0
    exits = ",".join(f"{key}:{value}" for key, value in sorted(stats.exits.items())) or "-"
    return (
        f"signals={stats.signals} entries={stats.entries} expired={stats.expired} nofill={stats.nofill} "
        f"W/L/F={stats.wins}/{stats.losses}/{stats.flats} WR={wr:.1f}% PF_USDT={pf} "
        f"pnl={stats.pnl_usdt:+.4f}USDT bank=${BANK.balance_usdt:.2f} "
        f"fill_med={fill_med:.1f}% notional_med=${notional_med:.0f} hold_med={hold_med:.0f}ms "
        f"signal_to_fill_med={signal_fill_med:.0f}ms exits={exits}"
    )


def _append_trade(path: Path, row: dict[str, object]) -> None:
    fields = [
        "signal_ms", "entry_ms", "exit_ms", "symbol", "direction", "requested_notional_usdt",
        "filled_notional_usdt", "fill_ratio", "leverage", "actual_margin_usdt", "entry_price", "exit_price",
        "entry_fee_usdt", "exit_fee_usdt", "pnl_bps", "pnl_usdt", "roe_pct", "mfe_bps", "mae_bps",
        "hold_ms", "signal_to_fill_ms", "exit_reason", "runner_armed", "liquidation_price",
        "entry_order_id", "exit_order_id",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    exists = path.exists()
    with path.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        if not exists:
            writer.writeheader()
        writer.writerow({name: row.get(name, "") for name in fields})


def _record(stats: Stats, pnl_usdt: float, reason: str, hold_ms: int) -> None:
    stats.pnl_usdt += pnl_usdt
    stats.holds.append(float(hold_ms))
    stats.exits[reason] = stats.exits.get(reason, 0) + 1
    if pnl_usdt > 1e-9:
        stats.wins += 1
        stats.gross_win_usdt += pnl_usdt
    elif pnl_usdt < -1e-9:
        stats.losses += 1
        stats.gross_loss_usdt += abs(pnl_usdt)
    else:
        stats.flats += 1


def _effective_leverage(live_max: int, demo_detail: dict) -> int:
    demo_max = max(1, int(demo_detail.get("maxLeverage") or 1))
    return max(1, min(int(auto.REQUESTED_LEVERAGE), max(1, int(live_max)), demo_max))


async def _close_and_account(
    adapter: MexcWebExecutionAdapter,
    pos: DemoPosition,
    stats: Stats,
    output: Path,
    reason: str,
) -> None:
    exit_start = time.time_ns() / 1_000_000.0
    exit_fill = await _close_position_fully(adapter, pos.remote)
    exit_done = time.time_ns() / 1_000_000.0
    exit_price = float(exit_fill.avg_price or pos.actual_entry_price)
    gross = pos.signal.direction * (exit_price - pos.actual_entry_price) * pos.remote.qty
    pnl = gross - float(pos.entry_fill.fee_usdt) - float(exit_fill.fee_usdt)
    pnl_bps = pnl / max(pos.filled_notional_usdt, 1e-12) * 10_000.0
    margin = pos.filled_notional_usdt / max(float(pos.remote.leverage), 1.0)
    roe = pnl / max(margin, 1e-12) * 100.0
    hold_ms = max(0, int(exit_done) - pos.entry_ms)
    before = BANK.balance_usdt
    BANK.balance_usdt = max(0.0, BANK.balance_usdt + pnl)
    _record(stats, pnl, reason, hold_ms)
    _append_trade(output, {
        "signal_ms": pos.signal_ms,
        "entry_ms": pos.entry_ms,
        "exit_ms": int(exit_done),
        "symbol": pos.signal.symbol,
        "direction": "LONG" if pos.signal.direction > 0 else "SHORT",
        "requested_notional_usdt": pos.requested_notional_usdt,
        "filled_notional_usdt": pos.filled_notional_usdt,
        "fill_ratio": pos.filled_notional_usdt / max(pos.requested_notional_usdt, 1e-12),
        "leverage": pos.remote.leverage,
        "actual_margin_usdt": margin,
        "entry_price": pos.actual_entry_price,
        "exit_price": exit_price,
        "entry_fee_usdt": pos.entry_fill.fee_usdt,
        "exit_fee_usdt": exit_fill.fee_usdt,
        "pnl_bps": pnl_bps,
        "pnl_usdt": pnl,
        "roe_pct": roe,
        "mfe_bps": pos.mfe_bps,
        "mae_bps": pos.mae_bps,
        "hold_ms": hold_ms,
        "signal_to_fill_ms": pos.entry_ms - pos.signal_ms,
        "exit_reason": reason,
        "runner_armed": pos.runner_armed,
        "liquidation_price": pos.remote.liquidation_price or "",
        "entry_order_id": pos.entry_fill.order_id,
        "exit_order_id": exit_fill.order_id,
    })
    console.print(
        f"[{'green' if pnl > 0 else 'red'}]TESTNET EXIT[/] {pos.signal.symbol} reason={reason} "
        f"pnl={pnl_bps:+.2f}bps ${pnl:+.2f} ROE={roe:+.1f}% "
        f"exit_submit_to_fill={exit_done-exit_start:.1f}ms bank=${before:.2f}->${BANK.balance_usdt:.2f}"
    )


async def run(args) -> None:
    auto._apply_immediate_exit_policy(args)
    BANK.balance_usdt = auto.START_BANK_USDT
    output = Path(args.testnet_output)
    if output.exists() and not args.append_output:
        output.unlink()

    candidates = await auto.discover(args)
    live_contracts = {row.contract.mexc_symbol: row.contract for row in candidates}
    selected_profiles = {row.profile.symbol: row.profile for row in candidates}

    cfg = WebExecutionConfig.demo_from_env(write_enabled=True)
    _assert_demo_write_config(cfg)

    stats = Stats()
    pos: DemoPosition | None = None
    wake = asyncio.Event()
    binance = mexc = None

    async with MexcWebExecutionAdapter(cfg) as adapter:
        await adapter.probe()
        existing = await adapter.get_positions()
        if existing:
            labels = ", ".join(f"{p.symbol}:{p.side.value}:{p.qty:g}" for p in existing[:10])
            raise MexcWebError(f"Testnet account already has open position(s): {labels}; refusing to mix sessions")

        demo_detail: dict[str, dict] = {}
        for symbol in list(live_contracts):
            try:
                detail = await adapter.get_contract_detail(symbol)
                if float(detail.get("contractSize") or 0) <= 0 or float(detail.get("priceUnit") or 0) <= 0:
                    continue
                demo_detail[symbol] = detail
            except Exception:
                continue

        symbols = [row.profile.symbol for row in candidates if row.profile.symbol in demo_detail]
        if not symbols:
            raise MexcWebError("None of the current AUTO candidates exists as the same symbol on MEXC Testnet")
        contracts = [live_contracts[symbol] for symbol in symbols]

        console.print("[bold cyan]CURRENT AUTO STRATEGY -> REAL MEXC TESTNET[/bold cyan]")
        console.print(f"Execution host hard-locked to https://{DEMO_HOST}; LIVE order writes are not used by this runner.")
        console.print("Signals/filters: LIVE Binance + LIVE MEXC; accepted orders: SAME symbol on MEXC Testnet.")
        console.print("No synthetic entry/exit sleep: actual Testnet IOC/close roundtrip is the execution latency.")
        console.print("Selected Testnet-compatible candidates: " + ", ".join(symbols))

        models = {
            c.mexc_symbol: MicroSpreadModel(
                horizon_ms=args.micro_horizon_ms,
                baseline_seconds=args.baseline_seconds,
                baseline_exclusion_ms=args.baseline_exclusion_ms,
                min_edge_bps=0.0,
                min_binance_move_bps=0.0,
                max_binance_age_ms=args.max_binance_age_ms,
                max_mexc_age_ms=args.max_mexc_age_ms,
            )
            for c in contracts
        }
        gate = LeadLagGate(
            noise_window_ms=args.noise_window_ms,
            residual_noise_multiplier=args.residual_noise_multiplier,
            binance_noise_multiplier=args.binance_noise_multiplier,
            min_edge_bps=args.min_edge_bps,
            min_net_edge_bps=args.min_net_edge_bps,
            spread_ratio=args.edge_to_spread_ratio,
            min_binance_move_bps=args.min_binance_move_bps,
            min_leader_advantage_bps=args.min_leader_advantage_bps,
            min_lead_ratio=args.min_lead_ratio,
            confirm_updates=args.confirm_updates,
            confirm_ms=args.confirm_ms,
            rearm_fraction=args.rearm_fraction,
        )
        binance = EventBinanceBookTickerFeed(contracts, models, wake)
        mexc = EventMexcDepthFeed(symbols, models, wake, depth_limit=args.depth_limit)
        await binance.start()
        await mexc.start()

        warmup_until = time.monotonic() + args.warmup_seconds
        deadline = time.monotonic() + args.session_seconds
        last_report = None
        try:
            while time.monotonic() < deadline and stats.signals < args.max_signals and (stats.wins + stats.losses + stats.flats) < args.target_closed_trades:
                now = time.monotonic()
                now_ms = int(time.time() * 1000)

                if pos is None and now >= warmup_until and BANK.balance_usdt > BANK.stop_balance:
                    rows = []
                    for symbol in symbols:
                        book = mexc.books.get(symbol)
                        if book is None or now_ms - book.recv_ms > args.max_book_age_ms:
                            continue
                        snap = models[symbol].snapshot(now_ms=now_ms, threshold_bps=0.0)
                        if not _valid_snapshot(snap):
                            continue
                        d = gate.observe(symbol, snap, book.spread_bps, now_ms, event_key=_event_key(models[symbol]))
                        if not d.ready:
                            continue
                        strength = abs(d.residual_bps) / max(d.threshold_bps, 1e-12)
                        if strength < args.min_signal_strength_ratio or abs(d.residual_bps) < args.min_absolute_residual_bps:
                            continue
                        rows.append((abs(d.residual_bps), strength, d.leader_advantage_bps, symbol, d, snap, book))

                    if rows:
                        _, strength, _, symbol, d, snap, book = max(rows, key=lambda x: (x[0], x[1], x[2]))
                        stats.signals += 1
                        signal_ms = int(time.time() * 1000)
                        signal = Signal(
                            signal_id=f"testnet-{stats.signals}-{signal_ms}", ts_ms=signal_ms, symbol=symbol,
                            direction=d.direction, residual_bps=d.residual_bps, threshold_bps=d.threshold_bps,
                            noise_bps=d.noise_bps, spread_bps=book.spread_bps,
                            leader_advantage_bps=d.leader_advantage_bps, binance_move_bps=d.binance_move_bps,
                            mexc_move_bps=d.mexc_move_bps, binance_price=snap.binance_mid, mexc_price=snap.mexc_mid,
                        )
                        console.print(
                            f"SIGNAL #{stats.signals} {symbol} {'LONG' if d.direction > 0 else 'SHORT'} "
                            f"residual={d.residual_bps:+.2f}bps strength={strength:.2f}x spread={book.spread_bps:.2f}bps"
                        )

                        ok, why, residual_ret, impulse_ret = auto._economic_arrival_entry_ok(
                            signal=signal, current_residual_bps=snap.edge_bps, current_binance_price=snap.binance_mid,
                            current_spread_bps=book.spread_bps, min_residual_retention=args.min_residual_retention,
                            min_impulse_retention=args.min_impulse_retention, min_remaining_edge_bps=args.min_absolute_residual_bps,
                            min_edge_after_spread_bps=args.min_edge_after_spread_bps,
                        )
                        if not ok:
                            stats.expired += 1
                            console.print(f"EXPIRED {symbol} reason={why} residual_ret={residual_ret:.1%} impulse_ret={impulse_ret:.1%}")
                        else:
                            live_contract = live_contracts[symbol]
                            leverage = _effective_leverage(live_contract.max_leverage, demo_detail[symbol])
                            requested, margin, reserve = _requested_notional(BANK, leverage)
                            console.print(
                                f"RISK SIZE {symbol} bank=${BANK.balance_usdt:.2f} historical_target_notional=${auto.LEGACY_TARGET_NOTIONAL_USDT:.0f} "
                                f"leverage={leverage}x required_margin=${auto.LEGACY_TARGET_NOTIONAL_USDT/leverage:.2f} "
                                f"allocated_margin=${margin:.2f} reserve=${reserve:.2f} requested_notional=${requested:.2f}"
                            )
                            planned = virtual_ioc_fill(
                                book, direction=signal.direction, target_notional_usdt=requested,
                                contract_size=live_contract.contract_size, cross_bps=args.ioc_cross_bps,
                            )
                            planned_notional = planned.qty * planned.avg_price
                            planned_slip = entry_slippage_bps(signal.direction, book, planned.avg_price)
                            if planned.qty <= 0 or planned_notional < args.min_filled_notional_usdt:
                                stats.nofill += 1
                            elif planned_slip > args.max_entry_slippage_bps + 1e-9:
                                stats.expired += 1
                                console.print(f"SKIP SLIP {symbol} planned_slip={planned_slip:.2f}bps")
                            else:
                                cost = immediate_roundtrip_cost_bps(
                                    book, direction=signal.direction, entry_price=planned.avg_price,
                                    qty=planned.qty, contract_size=live_contract.contract_size,
                                )
                                edge_ok, required = executable_edge_ok(
                                    snap.edge_bps, cost, args.min_executable_net_edge_bps, args.min_edge_to_cost_ratio,
                                )
                                if not edge_ok:
                                    stats.expired += 1
                                    console.print(f"SKIP COST {symbol} residual={abs(snap.edge_bps):.2f}bps cost={cost:.2f} required={required:.2f}")
                                else:
                                    side = OrderSide.LONG if signal.direction > 0 else OrderSide.SHORT
                                    demo_best = await adapter.get_best_price(symbol, side)
                                    price_unit = float(demo_detail[symbol].get("priceUnit") or 0)
                                    limit_price = _marketable_ioc_price(side, book, args.ioc_cross_bps, price_unit)
                                    # Keep the same requested-notional policy; Testnet determines actual IOC partial fill.
                                    requested_qty = requested / max(demo_best, 1e-12)
                                    marks: dict[str, float] = {}
                                    fill = await adapter.open_ioc(
                                        symbol=symbol, side=side, price=limit_price, qty=requested_qty, leverage=leverage,
                                        client_order_id=f"tn-entry-{uuid.uuid4().hex}"[:32], timing_marks=marks,
                                    )
                                    if fill.filled_qty <= 0:
                                        stats.nofill += 1
                                        console.print(f"TESTNET NO FILL {symbol} requested=${requested:.0f}")
                                    else:
                                        remote = await _resolve_remote_position(adapter, symbol, side, fill, leverage)
                                        entry_ms = int(time.time() * 1000)
                                        actual_entry = float(remote.entry_price or fill.avg_price or demo_best)
                                        filled_notional = remote.qty * actual_entry
                                        fill_ratio = filled_notional / max(requested, 1e-12)
                                        stats.entries += 1
                                        stats.fills.append(fill_ratio)
                                        stats.notionals.append(filled_notional)
                                        stats.signal_to_fill.append(float(entry_ms - signal_ms))
                                        fresh_book = mexc.books.get(symbol) or book
                                        fresh_snap = models[symbol].snapshot(now_ms=entry_ms, threshold_bps=0.0)
                                        pos = DemoPosition(
                                            signal=signal, remote=remote, entry_fill=fill, entry_ms=entry_ms,
                                            signal_ms=signal_ms, live_entry_mid=fresh_book.mid,
                                            live_entry_binance=fresh_snap.binance_mid,
                                            live_entry_residual_bps=fresh_snap.edge_bps,
                                            actual_entry_price=actual_entry, requested_notional_usdt=requested,
                                            filled_notional_usdt=filled_notional,
                                            trailing=PositiveTrailing(distance_bps=max(args.trailing_distance_bps, fresh_book.spread_bps)),
                                            last_progress_ms=entry_ms, last_position_poll_ms=entry_ms,
                                        )
                                        actual_margin = filled_notional / leverage
                                        liq = remote.liquidation_price
                                        liq_txt = f"{liq:.10g}" if liq else "unknown"
                                        liq_distance = (
                                            signal.direction * (actual_entry - float(liq)) / actual_entry * 10_000.0
                                            if liq and actual_entry > 0 else math.nan
                                        )
                                        console.print(
                                            f"TESTNET ENTRY {symbol} {'LONG' if signal.direction > 0 else 'SHORT'} "
                                            f"requested=${requested:.0f} filled=${filled_notional:.0f} ({fill_ratio:.1%}) "
                                            f"actual_margin=${actual_margin:.2f} lev={leverage}x signal_to_fill={entry_ms-signal_ms}ms "
                                            f"fee=${fill.fee_usdt:.6f} liq={liq_txt} "
                                            f"liq_distance={liq_distance:.1f}bps"
                                        )

                                        # Same arrival thesis check, now using the actual post-fill market state.
                                        post_ok, post_why, _, _ = auto._economic_arrival_entry_ok(
                                            signal=signal, current_residual_bps=fresh_snap.edge_bps,
                                            current_binance_price=fresh_snap.binance_mid,
                                            current_spread_bps=fresh_book.spread_bps,
                                            min_residual_retention=args.min_residual_retention,
                                            min_impulse_retention=args.min_impulse_retention,
                                            min_remaining_edge_bps=args.min_absolute_residual_bps,
                                            min_edge_after_spread_bps=args.min_edge_after_spread_bps,
                                        )
                                        actual_slip = max(0.0, _signed_move_bps(signal.direction, fresh_book.ask if signal.direction > 0 else fresh_book.bid, actual_entry))
                                        abort_reason = None
                                        if filled_notional < args.min_filled_notional_usdt:
                                            abort_reason = "actual_fill_too_small"
                                        elif actual_slip > args.max_entry_slippage_bps + 1e-9:
                                            abort_reason = "actual_entry_slippage"
                                        elif not post_ok:
                                            abort_reason = f"arrival_{post_why}"
                                        if abort_reason is not None:
                                            await _close_and_account(adapter, pos, stats, output, abort_reason)
                                            pos = None

                if pos is not None:
                    symbol = pos.signal.symbol
                    book = mexc.books.get(symbol)
                    if book is not None:
                        snap = models[symbol].snapshot(now_ms=now_ms, threshold_bps=0.0)
                        if _valid_snapshot(snap):
                            age_ms = now_ms - pos.entry_ms
                            mid_move = directional_move_bps(pos.signal.direction, pos.live_entry_mid, book.mid)
                            leader_move = directional_move_bps(pos.signal.direction, pos.live_entry_binance, snap.binance_mid)
                            residual_dir = 1 if snap.edge_bps > 0 else -1 if snap.edge_bps < 0 else 0
                            full_qty, exit_vwap = _exit_depth_for_qty(
                                book, direction=pos.signal.direction, qty=pos.remote.qty,
                                contract_size=live_contracts[symbol].contract_size,
                            )
                            executable_pnl_bps = (
                                _signed_move_bps(pos.signal.direction, pos.actual_entry_price, exit_vwap)
                                if full_qty + 1e-12 >= pos.remote.qty and exit_vwap > 0 else None
                            )
                            trail = pos.trailing.update(executable_pnl_bps) if executable_pnl_bps is not None else None
                            if executable_pnl_bps is not None:
                                pos.mfe_bps = max(pos.mfe_bps, executable_pnl_bps)
                                pos.mae_bps = min(pos.mae_bps, executable_pnl_bps)
                                if executable_pnl_bps >= args.min_progress_bps:
                                    pos.last_progress_ms = now_ms
                                if not pos.runner_armed and pos.trailing.peak_bps + 1e-9 >= args.profit_runner_arm_bps:
                                    pos.runner_armed = True
                                    console.print(
                                        f"[bold green]PROFIT RUNNER ARMED[/bold green] {symbol} peak={pos.trailing.peak_bps:.2f}bps "
                                        f"threshold={args.profit_runner_arm_bps:.2f}bps; convergence disabled; "
                                        f"trailing/reversal/emergency/Testnet liquidation remain active."
                                    )

                            reason = None
                            if age_ms >= args.min_hold_ms:
                                if mid_move <= -args.mid_adverse_cut_bps:
                                    reason = "mid_adverse_cut"
                                elif leader_move <= -args.leader_retrace_exit_bps:
                                    reason = "leader_retrace"
                                elif residual_dir == -pos.signal.direction and abs(snap.edge_bps) >= args.reversal_edge_bps:
                                    reason = "residual_reversal"
                                elif (not pos.runner_armed and abs(snap.edge_bps) <= max(args.convergence_bps, abs(pos.live_entry_residual_bps) * args.convergence_fraction) and mid_move >= args.min_catchup_bps):
                                    reason = "mexc_catchup_convergence"
                                elif age_ms >= args.no_progress_ms and mid_move < args.min_progress_bps:
                                    reason = "no_progress"
                                elif trail is not None and executable_pnl_bps is not None and executable_pnl_bps <= trail:
                                    reason = "positive_trailing_stop"
                                elif age_ms >= args.max_hold_ms:
                                    reason = "timeout"
                            if reason is not None:
                                console.print(f"EXIT DECISION {symbol} reason={reason} decision_hold={age_ms}ms -> submit Testnet close immediately")
                                await _close_and_account(adapter, pos, stats, output, reason)
                                pos = None

                    # Detect a Testnet-side liquidation or external disappearance.
                    if pos is not None and now_ms - pos.last_position_poll_ms >= args.testnet_position_poll_ms:
                        pos.last_position_poll_ms = now_ms
                        remote_now = await adapter.get_position(symbol)
                        if remote_now is None:
                            liq_price = float(pos.remote.liquidation_price or pos.actual_entry_price)
                            gross = pos.signal.direction * (liq_price - pos.actual_entry_price) * pos.remote.qty
                            pnl = gross - float(pos.entry_fill.fee_usdt)
                            margin = pos.filled_notional_usdt / max(float(pos.remote.leverage), 1.0)
                            pnl = max(pnl, -margin)
                            pnl_bps = pnl / max(pos.filled_notional_usdt, 1e-12) * 10_000.0
                            roe = pnl / max(margin, 1e-12) * 100.0
                            before = BANK.balance_usdt
                            BANK.balance_usdt = max(0.0, BANK.balance_usdt + pnl)
                            hold_ms = now_ms - pos.entry_ms
                            _record(stats, pnl, "mexc_demo_liquidation", hold_ms)
                            _append_trade(output, {
                                "signal_ms": pos.signal_ms, "entry_ms": pos.entry_ms, "exit_ms": now_ms,
                                "symbol": symbol, "direction": "LONG" if pos.signal.direction > 0 else "SHORT",
                                "requested_notional_usdt": pos.requested_notional_usdt,
                                "filled_notional_usdt": pos.filled_notional_usdt,
                                "fill_ratio": pos.filled_notional_usdt / max(pos.requested_notional_usdt, 1e-12),
                                "leverage": pos.remote.leverage, "actual_margin_usdt": margin,
                                "entry_price": pos.actual_entry_price, "exit_price": liq_price,
                                "entry_fee_usdt": pos.entry_fill.fee_usdt, "exit_fee_usdt": "unknown",
                                "pnl_bps": pnl_bps, "pnl_usdt": pnl, "roe_pct": roe,
                                "mfe_bps": pos.mfe_bps, "mae_bps": pos.mae_bps, "hold_ms": hold_ms,
                                "signal_to_fill_ms": pos.entry_ms - pos.signal_ms,
                                "exit_reason": "mexc_demo_liquidation", "runner_armed": pos.runner_armed,
                                "liquidation_price": pos.remote.liquidation_price or "",
                                "entry_order_id": pos.entry_fill.order_id, "exit_order_id": "exchange_forced",
                            })
                            console.print(
                                f"[bold red]MEXC TESTNET LIQUIDATION[/bold red] {symbol} liq={liq_price:.10g} "
                                f"estimated_pnl=${pnl:+.2f} ROE={roe:+.1f}% bank=${before:.2f}->${BANK.balance_usdt:.2f}"
                            )
                            pos = None

                report = (stats.signals, stats.entries, stats.expired, stats.nofill, stats.wins, stats.losses, stats.flats, round(stats.pnl_usdt, 6))
                if report != last_report:
                    console.print("STATE " + _summary(stats))
                    last_report = report

                wake.clear()
                try:
                    await asyncio.wait_for(wake.wait(), timeout=0.01)
                except TimeoutError:
                    pass
        finally:
            if pos is not None:
                try:
                    console.print(f"[bold yellow]SHUTDOWN CLEANUP[/bold yellow] closing Testnet {pos.signal.symbol}")
                    await _close_and_account(adapter, pos, stats, output, "shutdown_cleanup")
                    pos = None
                except Exception as exc:
                    console.print(f"[bold red]TESTNET CLEANUP FAILED[/bold red] {type(exc).__name__}: {exc}")
                    raise
            if binance is not None:
                await binance.close()
            if mexc is not None:
                await mexc.close()

    console.print("\n[bold]FINAL CURRENT-STRATEGY TESTNET REPORT[/bold]")
    console.print(_summary(stats))
    console.print(f"CSV: {output.resolve()}")
    console.print(f"TESTNET-ONLY CONFIRMED: execution adapter is hard-locked to {DEMO_HOST}; no LIVE write config is constructed.")


def build_parser():
    p = auto.build_parser()
    p.description = "Current AUTO persistent lag strategy with real same-symbol MEXC Testnet execution"
    p.add_argument("--profit-runner-arm-bps", type=float, default=5.0)
    p.add_argument("--testnet-position-poll-ms", type=int, default=250)
    p.add_argument("--testnet-output", default="persistent_end2end_TESTNET.csv")
    p.add_argument("--append-output", action="store_true")
    return p


def main() -> None:
    args = build_parser().parse_args()
    auto.apply_baseline_v1(args)
    if args.discovery_top <= 0 or args.profit_runner_arm_bps < 0 or args.target_closed_trades <= 0:
        raise SystemExit("invalid discovery/profit-runner/trade limit")
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        console.print("\n[yellow]Testnet stop requested.[/yellow]")
        raise SystemExit(130)
    except Exception as exc:
        console.print(f"[red]TESTNET RUNNER STOPPED:[/red] {type(exc).__name__}: {exc}")
        raise SystemExit(2)


if __name__ == "__main__":
    main()
