from __future__ import annotations

import argparse
import asyncio
import math
import statistics
import time
import uuid
from dataclasses import dataclass

from rich.console import Console

from . import prelive_persistent_ioc_shadow_v2 as v2
from .demo_hybrid_test import _flatten_position, _load_project_env, _reconcile_ioc_position
from .demo_smoke import _assert_demo_safety
from .demo_tick_test import _trade_pnl
from .execution import OrderSide, PositionSnapshot
from .lead_lag_strategy import LeadLagGate
from .live_lead_lag_shadow import PositiveTrailing
from .live_production_runner import _signed_move_bps
from .microspread import MicroSpreadModel
from .microspread_feed import EventBinanceBookTickerFeed, EventMexcDepthFeed
from .prelive_latency_diagnostic import _exit_depth_for_qty
from .prelive_measured_rtt_diagnostic import _percentile, measure_live_private_rtt
from .prelive_persistent_catchup_shadow import Signal, delayed_catchup_entry_ok, directional_move_bps
from .prelive_persistent_ioc_shadow import immediate_roundtrip_cost_bps, virtual_ioc_fill
from .testnet_known_good_v1 import (
    KNOWN_GOOD_COMMIT,
    Stats,
    _cross_limit,
    _entry_slippage_bps,
    _event_key,
    _max_base_qty,
    _measure_testnet_private_rtt,
    _same_symbol_universe,
    _summary,
    _valid_snapshot,
)
from .web_execution import MexcWebError, MexcWebExecutionAdapter, WebExecutionConfig

console = Console()


@dataclass(slots=True)
class Pending:
    signal: Signal
    execute_at: float
    signal_mono: float


@dataclass(slots=True)
class RiskPosition:
    signal: Signal
    remote: PositionSnapshot
    entry_ts_ms: int
    entry_mono: float
    entry_live_mid: float
    entry_binance_price: float
    entry_residual_bps: float
    entry_demo_price: float
    entry_fee_usdt: float
    entry_notional: float
    entry_fill_ratio: float
    trailing: PositiveTrailing
    theoretical_liq_price: float | None
    exit_reason: str | None = None
    last_remote_check_mono: float = 0.0
    remote_check_failures: int = 0


def _float_first(row: dict, keys: tuple[str, ...]) -> float | None:
    for key in keys:
        raw = row.get(key)
        if raw in (None, ""):
            continue
        try:
            value = float(raw)
        except (TypeError, ValueError):
            continue
        if math.isfinite(value):
            return value
    return None


def _mmr_from_detail(detail: dict) -> float | None:
    value = _float_first(
        detail,
        (
            "maintenanceMarginRate",
            "maintainMarginRate",
            "maintenanceMarginRatio",
            "maintainRate",
            "mmr",
        ),
    )
    if value is None or value < 0:
        return None
    # Some endpoints expose percentages while others expose fractions.
    if value > 1:
        value /= 100.0
    return value if 0 <= value < 1 else None


def _liq_fee_rate_from_detail(detail: dict) -> float:
    value = _float_first(detail, ("liquidationFeeRate", "liquidateFeeRate", "liquidationFee", "liqFeeRate"))
    if value is None or value < 0:
        return 0.0
    if value > 1:
        value /= 100.0
    return value if value < 1 else 0.0


def theoretical_isolated_liq_price(
    side: OrderSide,
    entry_price: float,
    leverage: int,
    maintenance_margin_rate: float | None,
    liquidation_fee_rate: float = 0.0,
) -> float | None:
    """MEXC isolated-margin liquidation approximation.

    MEXC liquidation condition is position margin + unrealized PnL <= maintenance margin + liquidation fee.
    Risk tiers can change MMR, therefore the remote Testnet liquidationPrice remains the source of truth after entry.
    """
    if entry_price <= 0 or leverage <= 0 or maintenance_margin_rate is None:
        return None
    mmr_total = max(0.0, maintenance_margin_rate + max(0.0, liquidation_fee_rate))
    margin_fraction = 1.0 / leverage
    if side is OrderSide.LONG:
        return entry_price * (1.0 - margin_fraction + mmr_total)
    return entry_price * (1.0 + margin_fraction - mmr_total)


def liquidation_distance_bps(side: OrderSide, executable_price: float, liquidation_price: float | None) -> float:
    if executable_price <= 0 or not liquidation_price or liquidation_price <= 0:
        return math.inf
    if side is OrderSide.LONG:
        return (executable_price - liquidation_price) / executable_price * 10_000.0
    return (liquidation_price - executable_price) / executable_price * 10_000.0


def adverse_roe_pct(side: OrderSide, entry_price: float, executable_price: float, leverage: int) -> float:
    if entry_price <= 0 or executable_price <= 0 or leverage <= 0:
        return 0.0
    direction = 1.0 if side is OrderSide.LONG else -1.0
    return direction * (executable_price - entry_price) / entry_price * leverage * 100.0


def basis_bps(live_price: float, testnet_price: float) -> float:
    if live_price <= 0 or testnet_price <= 0:
        return math.inf
    return abs(testnet_price - live_price) / live_price * 10_000.0


async def _emergency_flatten(
    adapter: MexcWebExecutionAdapter,
    position: RiskPosition,
    reason: str,
) -> tuple[float, float, float]:
    fresh = await adapter.get_position(position.signal.symbol)
    if fresh is None:
        raise MexcWebError(f"{reason}: Testnet position disappeared before emergency close")
    position.remote = fresh
    exit_fill = await _flatten_position(adapter, fresh, reason)
    fees = position.entry_fee_usdt + exit_fill.fee_usdt
    net, _, roe = _trade_pnl(
        fresh.side,
        position.entry_demo_price,
        exit_fill.avg_price,
        fresh.qty,
        fresh.leverage,
        fees,
    )
    zero_fee, _, _ = _trade_pnl(
        fresh.side,
        position.entry_demo_price,
        exit_fill.avg_price,
        fresh.qty,
        fresh.leverage,
        0.0,
    )
    return net, zero_fee, roe


def _record_close(stats: Stats, reason: str, net: float, zero_fee: float, hold_ms: float) -> None:
    stats.net_pnl_usdt += net
    stats.zero_fee_pnl_usdt += zero_fee
    stats.holds.append(hold_ms)
    stats.reasons[reason] = stats.reasons.get(reason, 0) + 1
    if net > 1e-9:
        stats.wins += 1
        stats.gross_win_usdt += net
    elif net < -1e-9:
        stats.losses += 1
        stats.gross_loss_usdt += abs(net)
    else:
        stats.flats += 1


async def run(args: argparse.Namespace) -> None:
    _load_project_env()
    cfg = WebExecutionConfig.demo_from_env(write_enabled=True)
    _assert_demo_safety(cfg)

    async with MexcWebExecutionAdapter(cfg) as adapter:
        existing = await adapter.get_positions()
        if existing:
            text = ",".join(f"{p.symbol}:{p.side.value}:{p.qty:g}" for p in existing)
            raise MexcWebError(f"Refusing start: Testnet already has open position(s): {text}")

        contracts, testnet_detail = await _same_symbol_universe(adapter)
        symbols = [c.mexc_symbol for c in contracts]
        by_symbol = {c.mexc_symbol: c for c in contracts}

        live_rtts = await measure_live_private_rtt(
            samples=args.rtt_samples,
            warmup_samples=args.rtt_warmup_samples,
            interval_ms=args.rtt_interval_ms,
        )
        live_target_rtt = statistics.median(live_rtts)
        testnet_rtts = await _measure_testnet_private_rtt(adapter, symbols[0])
        testnet_rtt = statistics.median(testnet_rtts)
        # Known-good paper delayed one measured RTT. The Testnet POST itself consumes network time,
        # therefore pre-submit wait uses a conservative one-way estimate rather than adding a second full RTT.
        pre_submit_wait_ms = max(0.0, live_target_rtt - testnet_rtt / 2.0)

        console.print("[bold cyan]KNOWN-GOOD 372c3b2 / LIQUIDATION-AWARE REAL TESTNET[/bold cyan]")
        console.print(f"Source strategy commit: {KNOWN_GOOD_COMMIT}")
        console.print(
            "Signal logic is the known-good arrival-book runner. Testnet changes ONLY execution/risk: "
            "same-symbol real IOC, actual dealVol/open position as truth, actual fees, liquidation watchdog and emergency reduce-only exit."
        )
        console.print(
            f"IOC invariant: request=${args.target_notional_usdt:.0f}; unfilled remainder cancels. "
            "A $2,000 real Testnet fill becomes a $2,000 managed position; there is NO top-up/chase/retry."
        )
        console.print(
            f"Risk: leverage<={args.risk_max_leverage}x, emergency ROE<={-args.max_adverse_roe_pct:.1f}%, "
            f"min liquidation buffer={args.min_liq_distance_bps:.0f}bps, max Testnet/LIVE basis={args.max_testnet_basis_bps:.0f}bps."
        )
        console.print(f"SAME-SYMBOL TESTNET UNIVERSE {len(symbols)}: " + ",".join(symbols))
        console.print(
            f"Known-good LIVE RTT median={live_target_rtt:.1f}ms p95={_percentile(live_rtts,.95):.1f}ms; "
            f"Testnet private RTT median={testnet_rtt:.1f}ms; pre-submit wait={pre_submit_wait_ms:.1f}ms."
        )

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

        wake = asyncio.Event()
        binance = EventBinanceBookTickerFeed(contracts, models, wake)
        mexc = EventMexcDepthFeed(symbols, models, wake, depth_limit=args.depth_limit)
        await binance.start()
        await mexc.start()

        pending: Pending | None = None
        pos: RiskPosition | None = None
        stats = Stats()
        deadline = time.monotonic() + args.session_seconds
        warmup_until = time.monotonic() + args.warmup_seconds
        last_report_key: tuple | None = None

        try:
            while time.monotonic() < deadline and stats.signals < args.max_signals and stats.closed < args.target_closed_trades:
                now = time.monotonic()
                now_ms = int(time.time() * 1000)

                if pending is not None and pos is None and now >= pending.execute_at:
                    sig = pending.signal
                    signal_mono = pending.signal_mono
                    pending = None
                    current = mexc.books.get(sig.symbol)
                    snap = models[sig.symbol].snapshot(now_ms=now_ms, threshold_bps=0.0)
                    if current is None or now_ms - current.recv_ms > args.max_book_age_ms or not _valid_snapshot(snap):
                        stats.expired += 1
                    else:
                        ok, why, residual_ret, impulse_ret = delayed_catchup_entry_ok(
                            signal=sig,
                            current_residual_bps=snap.edge_bps,
                            current_binance_price=snap.binance_mid,
                            current_spread_bps=current.spread_bps,
                            min_residual_retention=args.min_residual_retention,
                            min_impulse_retention=args.min_impulse_retention,
                            min_remaining_edge_bps=args.min_absolute_residual_bps,
                            min_edge_after_spread_bps=args.min_edge_after_spread_bps,
                        )
                        if not ok:
                            stats.expired += 1
                            console.print(
                                f"EXPIRED {sig.symbol} reason={why} residual_ret={residual_ret:.1%} impulse_ret={impulse_ret:.1%}"
                            )
                        else:
                            contract = by_symbol[sig.symbol]
                            # Preserve the exact known-good arrival-book feasibility/cost gate.
                            planned = virtual_ioc_fill(
                                current,
                                direction=sig.direction,
                                target_notional_usdt=args.target_notional_usdt,
                                contract_size=contract.contract_size,
                                cross_bps=args.ioc_cross_bps,
                            )
                            planned_notional = planned.qty * planned.avg_price
                            planned_slip = v2.entry_slippage_bps(sig.direction, current, planned.avg_price)
                            if planned.qty <= 0 or planned_notional < args.min_filled_notional_usdt:
                                stats.no_fill += 1
                            elif planned_slip > args.max_entry_slippage_bps + 1e-9:
                                stats.expired += 1
                            else:
                                cost = immediate_roundtrip_cost_bps(
                                    current,
                                    direction=sig.direction,
                                    entry_price=planned.avg_price,
                                    qty=planned.qty,
                                    contract_size=contract.contract_size,
                                )
                                edge_ok, required = v2.executable_edge_ok(
                                    snap.edge_bps,
                                    cost,
                                    args.min_executable_net_edge_bps,
                                    args.min_edge_to_cost_ratio,
                                )
                                if not edge_ok:
                                    stats.expired += 1
                                else:
                                    side = OrderSide.LONG if sig.direction > 0 else OrderSide.SHORT
                                    testnet_best = await adapter.get_best_price(sig.symbol, side)
                                    live_best = current.ask if side is OrderSide.LONG else current.bid
                                    entry_basis = basis_bps(live_best, testnet_best)
                                    if entry_basis > args.max_testnet_basis_bps:
                                        stats.expired += 1
                                        console.print(
                                            f"[yellow]SKIP TESTNET BASIS[/yellow] {sig.symbol} basis={entry_basis:.2f}bps "
                                            f"limit={args.max_testnet_basis_bps:.2f}bps live={live_best:g} testnet={testnet_best:g}"
                                        )
                                        continue

                                    detail = testnet_detail[sig.symbol]
                                    limit_price = _cross_limit(testnet_best, side, args.ioc_cross_bps, detail)
                                    target_qty = args.target_notional_usdt / testnet_best
                                    max_qty = _max_base_qty(detail)
                                    if max_qty is not None:
                                        target_qty = min(target_qty, max_qty)
                                    exchange_max_leverage = max(1, int(detail.get("maxLeverage") or 1))
                                    leverage = min(exchange_max_leverage, max(1, int(args.risk_max_leverage)))
                                    marks: dict[str, float] = {}
                                    submit_start = time.monotonic()
                                    fill = await adapter.open_ioc(
                                        symbol=sig.symbol,
                                        side=side,
                                        price=limit_price,
                                        qty=target_qty,
                                        leverage=leverage,
                                        client_order_id=f"kgr-{uuid.uuid4().hex}",
                                        timing_marks=marks,
                                    )
                                    remote = await _reconcile_ioc_position(adapter, sig.symbol, side, fill)
                                    if remote is None:
                                        stats.no_fill += 1
                                        console.print(
                                            f"DEMO NO FILL {sig.symbol} requested=${args.target_notional_usdt:.0f} "
                                            f"dealVol=${fill.filled_qty * max(fill.avg_price, testnet_best):.0f}"
                                        )
                                    else:
                                        actual_entry = remote.entry_price or fill.avg_price or testnet_best
                                        actual_notional = remote.qty * actual_entry
                                        actual_slip = _entry_slippage_bps(side, testnet_best, actual_entry)
                                        fill_ratio = actual_notional / max(args.target_notional_usdt, 1e-12)
                                        mmr = _mmr_from_detail(detail)
                                        liq_fee_rate = _liq_fee_rate_from_detail(detail)
                                        theoretical_liq = theoretical_isolated_liq_price(
                                            side, actual_entry, remote.leverage, mmr, liq_fee_rate
                                        )
                                        exit_side = OrderSide.SHORT if side is OrderSide.LONG else OrderSide.LONG
                                        executable_now = await adapter.get_best_price(sig.symbol, exit_side)
                                        liq_distance = liquidation_distance_bps(
                                            side, executable_now, remote.liquidation_price
                                        )
                                        if actual_notional < args.min_filled_notional_usdt:
                                            stats.aborts += 1
                                            net, zero_fee, _ = await _emergency_flatten(
                                                adapter,
                                                RiskPosition(sig, remote, now_ms, time.monotonic(), current.mid, snap.binance_mid,
                                                             snap.edge_bps, actual_entry, fill.fee_usdt, actual_notional, fill_ratio,
                                                             PositiveTrailing(distance_bps=max(args.trailing_distance_bps,current.spread_bps)),
                                                             theoretical_liq),
                                                "below_min_filled_notional",
                                            )
                                            stats.abort_pnl_usdt += net
                                        elif actual_slip > args.max_entry_slippage_bps + 1e-9:
                                            stats.aborts += 1
                                            net, zero_fee, _ = await _emergency_flatten(
                                                adapter,
                                                RiskPosition(sig, remote, now_ms, time.monotonic(), current.mid, snap.binance_mid,
                                                             snap.edge_bps, actual_entry, fill.fee_usdt, actual_notional, fill_ratio,
                                                             PositiveTrailing(distance_bps=max(args.trailing_distance_bps,current.spread_bps)),
                                                             theoretical_liq),
                                                "actual_slippage_exceeded",
                                            )
                                            stats.abort_pnl_usdt += net
                                        elif liq_distance < args.min_liq_distance_bps:
                                            stats.aborts += 1
                                            net, zero_fee, roe = await _emergency_flatten(
                                                adapter,
                                                RiskPosition(sig, remote, now_ms, time.monotonic(), current.mid, snap.binance_mid,
                                                             snap.edge_bps, actual_entry, fill.fee_usdt, actual_notional, fill_ratio,
                                                             PositiveTrailing(distance_bps=max(args.trailing_distance_bps,current.spread_bps)),
                                                             theoretical_liq),
                                                "unsafe_liquidation_buffer",
                                            )
                                            stats.abort_pnl_usdt += net
                                            console.print(f"ABORT ROE={roe:+.2f}% liq_distance={liq_distance:.1f}bps")
                                        else:
                                            stats.entries += 1
                                            stats.fills.append(fill_ratio)
                                            stats.notionals.append(actual_notional)
                                            pos = RiskPosition(
                                                signal=sig,
                                                remote=remote,
                                                entry_ts_ms=int(time.time() * 1000),
                                                entry_mono=time.monotonic(),
                                                entry_live_mid=current.mid,
                                                entry_binance_price=snap.binance_mid,
                                                entry_residual_bps=snap.edge_bps,
                                                entry_demo_price=actual_entry,
                                                entry_fee_usdt=fill.fee_usdt,
                                                entry_notional=actual_notional,
                                                entry_fill_ratio=fill_ratio,
                                                trailing=PositiveTrailing(
                                                    distance_bps=max(args.trailing_distance_bps, current.spread_bps)
                                                ),
                                                theoretical_liq_price=theoretical_liq,
                                                last_remote_check_mono=time.monotonic(),
                                            )
                                            post_ms = marks.get("ioc_post_response_ms", 0) - marks.get("ioc_post_start_ms", 0)
                                            confirm_ms = marks.get("ioc_confirmed_ms", 0) - marks.get("ioc_post_start_ms", 0)
                                            console.print(
                                                f"[green]DEMO ENTRY[/green] {sig.symbol} {side.value.upper()} "
                                                f"requested=${args.target_notional_usdt:.0f} ACTUAL_FILLED=${actual_notional:.0f} ({fill_ratio:.1%}) "
                                                f"qty={remote.qty:g} entry={actual_entry:g} leverage={remote.leverage}x fee={fill.fee_usdt:.6f} "
                                                f"liq_remote={remote.liquidation_price or 0:g} liq_calc={theoretical_liq or 0:g} "
                                                f"liq_distance={liq_distance:.1f}bps basis={entry_basis:.1f}bps "
                                                f"signal_to_submit={(submit_start-signal_mono)*1000:.1f}ms post={post_ms:.1f}ms confirm={confirm_ms:.1f}ms"
                                            )

                if pos is not None:
                    book = mexc.books.get(pos.signal.symbol)
                    snap = models[pos.signal.symbol].snapshot(now_ms=now_ms, threshold_bps=0.0)
                    if book is not None and _valid_snapshot(snap):
                        age_ms = now_ms - pos.entry_ts_ms

                        # Independent Testnet risk watchdog. It does not alter alpha exits; it can only flatten earlier.
                        if now - pos.last_remote_check_mono >= args.risk_poll_ms / 1000.0:
                            pos.last_remote_check_mono = now
                            try:
                                fresh = await adapter.get_position(pos.signal.symbol)
                                if fresh is None:
                                    raise MexcWebError("remote_position_missing")
                                pos.remote = fresh
                                pos.remote_check_failures = 0
                                exit_side = OrderSide.SHORT if fresh.side is OrderSide.LONG else OrderSide.LONG
                                testnet_exit = await adapter.get_best_price(pos.signal.symbol, exit_side)
                                liq_dist = liquidation_distance_bps(fresh.side, testnet_exit, fresh.liquidation_price)
                                roe_now = adverse_roe_pct(
                                    fresh.side, pos.entry_demo_price, testnet_exit, fresh.leverage
                                )
                                if liq_dist <= args.emergency_liq_distance_bps:
                                    pos.exit_reason = "emergency_liquidation_buffer"
                                elif roe_now <= -abs(args.max_adverse_roe_pct):
                                    pos.exit_reason = "emergency_max_adverse_roe"
                            except Exception as exc:
                                pos.remote_check_failures += 1
                                if pos.remote_check_failures >= args.max_risk_poll_failures:
                                    pos.exit_reason = "emergency_risk_monitor_failure"
                                    console.print(f"[red]RISK WATCHDOG[/red] {pos.signal.symbol}: {exc}")

                        mid_move = directional_move_bps(pos.signal.direction, pos.entry_live_mid, book.mid)
                        leader_move = directional_move_bps(pos.signal.direction, pos.entry_binance_price, snap.binance_mid)
                        conv = max(args.convergence_bps, abs(pos.entry_residual_bps) * args.convergence_fraction)
                        residual_dir = 1 if snap.edge_bps > 0 else -1 if snap.edge_bps < 0 else 0
                        full_filled, full_exit = _exit_depth_for_qty(
                            book,
                            direction=pos.signal.direction,
                            qty=pos.remote.qty,
                            contract_size=by_symbol[pos.signal.symbol].contract_size,
                        )
                        executable_pnl_bps = (
                            _signed_move_bps(pos.signal.direction, pos.entry_live_mid, full_exit)
                            if full_filled + 1e-12 >= pos.remote.qty and full_exit > 0 else None
                        )
                        trail = pos.trailing.update(executable_pnl_bps) if executable_pnl_bps is not None else None

                        # Preserve exact known-good strategy exit priority unless emergency watchdog already fired.
                        if pos.exit_reason is None and age_ms >= args.min_hold_ms:
                            if mid_move <= -args.mid_adverse_cut_bps:
                                pos.exit_reason = "mid_adverse_cut"
                            elif leader_move <= -args.leader_retrace_exit_bps:
                                pos.exit_reason = "leader_retrace"
                            elif residual_dir == -pos.signal.direction and abs(snap.edge_bps) >= args.reversal_edge_bps:
                                pos.exit_reason = "residual_reversal"
                            elif abs(snap.edge_bps) <= conv and mid_move >= args.min_catchup_bps:
                                pos.exit_reason = "mexc_catchup_convergence"
                            elif age_ms >= args.no_progress_ms and mid_move < args.min_progress_bps:
                                pos.exit_reason = "no_progress"
                            elif trail is not None and executable_pnl_bps is not None and executable_pnl_bps <= trail:
                                pos.exit_reason = "positive_trailing_stop"
                            elif age_ms >= args.max_hold_ms:
                                pos.exit_reason = "timeout"

                        if pos.exit_reason is not None:
                            reason = pos.exit_reason
                            try:
                                net, zero_fee, roe = await _emergency_flatten(adapter, pos, reason)
                            except MexcWebError as exc:
                                # Missing remote position can mean Testnet liquidation/forced close. Do not hide it.
                                if "disappeared" in str(exc) or "missing" in str(exc):
                                    console.print(f"[red]REMOTE POSITION LOST[/red] {pos.signal.symbol}: {exc}")
                                    net = -pos.entry_notional / max(1, pos.remote.leverage)
                                    zero_fee = net
                                    roe = -100.0
                                    reason = "remote_forced_close_or_liquidation"
                                else:
                                    raise
                            _record_close(stats, reason, net, zero_fee, float(age_ms))
                            console.print(
                                f"[{'green' if net > 0 else 'red'}]DEMO EXIT[/] {pos.signal.symbol} {reason} "
                                f"NET=${net:+.6f} ZERO_FEE=${zero_fee:+.6f} ROE={roe:+.2f}% hold={age_ms}ms"
                            )
                            pos = None

                if now >= warmup_until and pending is None and pos is None:
                    candidates = []
                    for symbol in symbols:
                        book = mexc.books.get(symbol)
                        if book is None or now_ms - book.recv_ms > args.max_book_age_ms:
                            continue
                        model = models[symbol]
                        snap = model.snapshot(now_ms=now_ms, threshold_bps=0.0)
                        if not _valid_snapshot(snap):
                            continue
                        decision = gate.observe(symbol, snap, book.spread_bps, now_ms, event_key=_event_key(model))
                        if not decision.ready:
                            continue
                        strength = abs(decision.residual_bps) / max(decision.threshold_bps, 1e-12)
                        if strength < args.min_signal_strength_ratio or abs(decision.residual_bps) < args.min_absolute_residual_bps:
                            continue
                        candidates.append(
                            (abs(decision.residual_bps), strength, decision.leader_advantage_bps, symbol, decision, snap, book)
                        )
                    if candidates:
                        _, strength, _, symbol, decision, snap, book = max(
                            candidates, key=lambda row: (row[0], row[1], row[2])
                        )
                        stats.signals += 1
                        sig = Signal(
                            signal_id=f"kgr-{stats.signals}-{now_ms}",
                            ts_ms=now_ms,
                            symbol=symbol,
                            direction=decision.direction,
                            residual_bps=decision.residual_bps,
                            threshold_bps=decision.threshold_bps,
                            noise_bps=decision.noise_bps,
                            spread_bps=book.spread_bps,
                            leader_advantage_bps=decision.leader_advantage_bps,
                            binance_move_bps=decision.binance_move_bps,
                            mexc_move_bps=decision.mexc_move_bps,
                            binance_price=snap.binance_mid,
                            mexc_price=snap.mexc_mid,
                        )
                        signal_mono = time.monotonic()
                        pending = Pending(
                            signal=sig,
                            execute_at=signal_mono + pre_submit_wait_ms / 1000.0,
                            signal_mono=signal_mono,
                        )
                        console.print(
                            f"SIGNAL #{stats.signals} {symbol} {'LONG' if decision.direction > 0 else 'SHORT'} "
                            f"residual={decision.residual_bps:+.2f}bps strength={strength:.2f}x live_spread={book.spread_bps:.2f}bps"
                        )

                report_key = (
                    stats.signals, stats.entries, stats.expired, stats.no_fill, stats.aborts,
                    stats.closed, round(stats.net_pnl_usdt, 6)
                )
                if report_key != last_report_key:
                    console.print("STATE " + _summary(stats, args.target_closed_trades))
                    last_report_key = report_key

                wake.clear()
                try:
                    await asyncio.wait_for(wake.wait(), timeout=0.02)
                except TimeoutError:
                    pass
        finally:
            if pos is not None:
                try:
                    fresh = await adapter.get_position(pos.signal.symbol)
                    if fresh is not None:
                        await _flatten_position(adapter, fresh, "shutdown_cleanup")
                except Exception as exc:
                    console.print(f"[red]CLEANUP FAILED[/red] {pos.signal.symbol}: {exc}")
            await binance.close()
            await mexc.close()

        console.print("\n[bold]FINAL LIQUIDATION-AWARE REAL TESTNET REPORT[/bold]")
        console.print(_summary(stats, args.target_closed_trades))


def build_parser() -> argparse.ArgumentParser:
    p = v2.build_parser()
    p.description = "Known-good 372c3b2 strategy with real partial-IOC Testnet execution and liquidation-aware risk control"
    p.add_argument("--target-closed-trades", type=int, default=100)
    p.add_argument("--risk-max-leverage", type=int, default=10,
                   help="Execution-only safety cap; alpha/entry/exit strategy is unchanged")
    p.add_argument("--min-liq-distance-bps", type=float, default=500.0,
                   help="Abort immediately after entry if remote liquidation buffer is smaller")
    p.add_argument("--emergency-liq-distance-bps", type=float, default=300.0,
                   help="Emergency flatten if liquidation buffer later falls below this")
    p.add_argument("--max-adverse-roe-pct", type=float, default=8.0,
                   help="Independent emergency Testnet loss cap; strategy stop remains unchanged")
    p.add_argument("--risk-poll-ms", type=float, default=100.0)
    p.add_argument("--max-risk-poll-failures", type=int, default=5)
    p.add_argument("--max-testnet-basis-bps", type=float, default=50.0,
                   help="Skip entry if Testnet executable price is too detached from LIVE MEXC")
    return p


def main() -> None:
    args = build_parser().parse_args()
    if args.target_closed_trades <= 0 or args.risk_max_leverage <= 0:
        raise SystemExit("target_closed_trades and risk_max_leverage must be positive")
    if args.emergency_liq_distance_bps >= args.min_liq_distance_bps:
        raise SystemExit("emergency_liq_distance_bps must be lower than min_liq_distance_bps")
    try:
        asyncio.run(run(args))
    except MexcWebError as exc:
        console.print(f"[red]TESTNET RISK RUN FAILED:[/red] {exc}")
        raise SystemExit(2) from exc


if __name__ == "__main__":
    main()
