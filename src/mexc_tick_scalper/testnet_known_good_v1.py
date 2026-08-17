from __future__ import annotations

import argparse
import asyncio
import math
import statistics
import time
import uuid
from dataclasses import dataclass, field
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR

from rich.console import Console

from . import prelive_persistent_ioc_shadow_v2 as v2
from .demo_hybrid_test import _flatten_position, _load_project_env, _reconcile_ioc_position
from .demo_smoke import _assert_demo_safety
from .demo_tick_test import _trade_pnl
from .execution import OrderFill, OrderSide, PositionSnapshot
from .lead_lag import fetch_binance_usdm_symbols, mexc_to_binance_symbol
from .lead_lag_strategy import LeadLagGate
from .live_lead_lag_shadow import PositiveTrailing
from .live_production_runner import _signed_move_bps
from .live_zero_fee_universe import LiveZeroFeeContract
from .market import MexcPublicMarket
from .microspread import MicroSpreadModel
from .microspread_feed import EventBinanceBookTickerFeed, EventMexcDepthFeed, LiveBook
from .prelive_latency_diagnostic import _exit_depth_for_qty
from .prelive_measured_rtt_diagnostic import _percentile, measure_live_private_rtt
from .prelive_persistent_catchup_shadow import Signal, delayed_catchup_entry_ok, directional_move_bps
from .prelive_persistent_ioc_shadow import immediate_roundtrip_cost_bps, virtual_ioc_fill
from .web_execution import MexcWebError, MexcWebExecutionAdapter, WebExecutionConfig

console = Console()
LIVE_REST = "https://contract.mexc.com"
LIVE_WS = "wss://contract.mexc.com/edge"
KNOWN_GOOD_COMMIT = "372c3b286eb82aa4b87d806999f8db47173a2b3e"


@dataclass(slots=True)
class Pending:
    signal: Signal
    execute_at: float
    signal_mono: float


@dataclass(slots=True)
class Position:
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
    exit_reason: str | None = None


@dataclass(slots=True)
class Stats:
    signals: int = 0
    entries: int = 0
    expired: int = 0
    no_fill: int = 0
    aborts: int = 0
    wins: int = 0
    losses: int = 0
    flats: int = 0
    net_pnl_usdt: float = 0.0
    zero_fee_pnl_usdt: float = 0.0
    abort_pnl_usdt: float = 0.0
    gross_win_usdt: float = 0.0
    gross_loss_usdt: float = 0.0
    fills: list[float] = field(default_factory=list)
    notionals: list[float] = field(default_factory=list)
    holds: list[float] = field(default_factory=list)
    reasons: dict[str, int] = field(default_factory=dict)

    @property
    def closed(self) -> int:
        return self.wins + self.losses + self.flats

    @property
    def pf(self) -> float:
        if self.gross_loss_usdt <= 0:
            return math.inf if self.gross_win_usdt > 0 else 0.0
        return self.gross_win_usdt / self.gross_loss_usdt


def _event_key(model: MicroSpreadModel) -> tuple[int, int] | None:
    if not model.binance or not model.mexc:
        return None
    return int(model.binance[-1][0]), int(model.mexc[-1][0])


def _valid_snapshot(snap) -> bool:
    return snap.reason not in {"warming_up", "warming_baseline", "warming_horizon", "stale_binance", "stale_mexc"}


def _summary(s: Stats, target: int) -> str:
    wr = s.wins / s.closed * 100 if s.closed else 0.0
    pf = "inf" if math.isinf(s.pf) else f"{s.pf:.3f}"
    fill = statistics.median(s.fills) * 100 if s.fills else 0.0
    notional = statistics.median(s.notionals) if s.notionals else 0.0
    hold = statistics.median(s.holds) if s.holds else 0.0
    reasons = ",".join(f"{k}:{v}" for k, v in sorted(s.reasons.items())) or "-"
    return (
        f"signals={s.signals} entries={s.entries} expired={s.expired} nofill={s.no_fill} aborts={s.aborts} "
        f"closed={s.closed}/{target} W/L/F={s.wins}/{s.losses}/{s.flats} WR={wr:.1f}% PF_USDT={pf} "
        f"NET_AFTER_DEMO_FEES={s.net_pnl_usdt:+.6f}USDT ZERO_FEE_COUNTERFACTUAL={s.zero_fee_pnl_usdt:+.6f}USDT "
        f"abort_pnl={s.abort_pnl_usdt:+.6f} fill_med={fill:.1f}% notional_med=${notional:.0f} hold_med={hold:.0f}ms exits={reasons}"
    )


def _price_unit(detail: dict) -> Decimal:
    raw = detail.get("priceUnit")
    if raw not in (None, ""):
        try:
            unit = Decimal(str(raw))
            if unit > 0:
                return unit
        except Exception:
            pass
    try:
        scale = int(detail.get("priceScale"))
        if scale >= 0:
            return Decimal(1).scaleb(-scale)
    except Exception:
        pass
    return Decimal("0")


def _cross_limit(best: float, side: OrderSide, cross_bps: float, detail: dict) -> float:
    raw = Decimal(str(best))
    delta = Decimal(str(cross_bps)) / Decimal("10000")
    raw *= Decimal("1") + delta if side is OrderSide.LONG else Decimal("1") - delta
    unit = _price_unit(detail)
    if unit <= 0:
        return float(raw)
    rounding = ROUND_CEILING if side is OrderSide.LONG else ROUND_FLOOR
    return float((raw / unit).to_integral_value(rounding=rounding) * unit)


def _max_base_qty(detail: dict) -> float | None:
    try:
        max_vol = float(detail.get("maxVol") or 0)
        contract_size = float(detail.get("contractSize") or 0)
        if max_vol > 0 and contract_size > 0:
            return max_vol * contract_size
    except Exception:
        pass
    return None


def _entry_slippage_bps(side: OrderSide, best: float, avg: float) -> float:
    if best <= 0 or avg <= 0:
        return math.inf
    direction = 1 if side is OrderSide.LONG else -1
    return max(0.0, _signed_move_bps(direction, best, avg))


async def _testnet_contract_rows(adapter: MexcWebExecutionAdapter) -> dict[str, dict]:
    response = await adapter._request("GET", "/contract/detail")
    data = response.get("data", []) if isinstance(response, dict) else []
    if isinstance(data, dict):
        data = [data]
    out: dict[str, dict] = {}
    for row in data or []:
        symbol = str(row.get("symbol") or "").upper()
        if symbol:
            out[symbol] = row
    return out


async def _same_symbol_universe(adapter: MexcWebExecutionAdapter) -> tuple[list[LiveZeroFeeContract], dict[str, dict]]:
    testnet_rows = await _testnet_contract_rows(adapter)
    binance_symbols = await fetch_binance_usdm_symbols()
    live_rows = await MexcPublicMarket(LIVE_REST, LIVE_WS).contracts()
    out: list[LiveZeroFeeContract] = []
    details: dict[str, dict] = {}
    for row in live_rows:
        symbol = str(row.get("symbol") or "").upper()
        if not symbol or symbol not in testnet_rows:
            continue
        bsymbol = mexc_to_binance_symbol(symbol)
        if bsymbol not in binance_symbols:
            continue
        contract_size = float(row.get("contractSize") or 0)
        min_vol = float(row.get("minVol") or 0)
        if contract_size <= 0 or min_vol <= 0:
            continue
        # Require a real two-sided Testnet book now; this is execution availability, not alpha ranking.
        try:
            ask, bid = await asyncio.gather(
                adapter.get_best_price(symbol, OrderSide.LONG),
                adapter.get_best_price(symbol, OrderSide.SHORT),
            )
        except MexcWebError:
            continue
        if ask <= 0 or bid <= 0 or ask < bid:
            continue
        out.append(
            LiveZeroFeeContract(
                mexc_symbol=symbol,
                binance_symbol=bsymbol,
                max_leverage=int(row.get("maxLeverage") or 1),
                contract_size=contract_size,
                min_vol=min_vol,
            )
        )
        details[symbol] = testnet_rows[symbol]
    if not out:
        raise MexcWebError("No same-symbol contract currently has Binance USD-M + LIVE MEXC + usable MEXC Testnet book")
    return out, details


async def _measure_testnet_private_rtt(adapter: MexcWebExecutionAdapter, symbol: str, samples: int = 12) -> list[float]:
    values: list[float] = []
    for _ in range(max(3, samples)):
        t0 = time.perf_counter_ns()
        await adapter.get_position(symbol)
        values.append((time.perf_counter_ns() - t0) / 1_000_000.0)
        await asyncio.sleep(0.05)
    return values


async def _abort_position(
    adapter: MexcWebExecutionAdapter,
    remote: PositionSnapshot,
    entry_price: float,
    entry_fee: float,
    reason: str,
) -> float:
    exit_fill = await _flatten_position(adapter, remote, reason)
    fees = entry_fee + exit_fill.fee_usdt
    pnl, _, _ = _trade_pnl(remote.side, entry_price, exit_fill.avg_price, remote.qty, remote.leverage, fees)
    console.print(
        f"[yellow]DEMO ABORT[/yellow] {remote.symbol} {reason} qty={remote.qty:g} "
        f"entry={entry_price:g} exit={exit_fill.avg_price:g} fees={fees:.6f} net=${pnl:+.6f}"
    )
    return pnl


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
        # The known-good paper model delayed entry by one full private RTT. A real Testnet order already
        # spends network time travelling to the exchange, so wait only the residual budget. Half of a
        # measured private RTT is the best observable one-way estimate available from this interface.
        pre_submit_wait_ms = max(0.0, live_target_rtt - testnet_rtt / 2.0)

        console.print("[bold cyan]KNOWN-GOOD 372c3b2 -> REAL MEXC TESTNET[/bold cyan]")
        console.print(f"Source strategy commit: {KNOWN_GOOD_COMMIT}")
        console.print(
            "LIVE Binance + LIVE MEXC generate the SAME lead-lag decisions as the proven runner. "
            "Accepted entries send a REAL IOC on the SAME symbol to MEXC Testnet; no paper child, mirror or proxy symbol."
        )
        console.print(
            "Testnet cannot reproduce the original exact-0/0 persistent-pair universe, so only that pair-eligibility layer is adapted: "
            "we scan exact same-symbol contracts available on Binance USD-M + LIVE MEXC + Testnet. All signal/retention/IOC/cost/exit thresholds remain the 372c3b2 defaults."
        )
        console.print(f"SAME-SYMBOL TESTNET UNIVERSE {len(symbols)}: " + ",".join(symbols))
        console.print(
            f"Known-good LIVE RTT target median={live_target_rtt:.1f}ms p95={_percentile(live_rtts, .95):.1f}ms; "
            f"Testnet private RTT median={testnet_rtt:.1f}ms; pre-submit compensation wait={pre_submit_wait_ms:.1f}ms."
        )
        console.print(
            f"Frozen gates: residual>={args.min_absolute_residual_bps:.1f}bps strength>={args.min_signal_strength_ratio:.1f}x "
            f"IOC cross<={args.ioc_cross_bps:.2f}bps slippage<={args.max_entry_slippage_bps:.2f}bps "
            f"cost+{args.min_executable_net_edge_bps:.1f}bps/{args.min_edge_to_cost_ratio:.1f}x."
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
        pos: Position | None = None
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
                                console.print(f"SKIP SLIP {sig.symbol} planned={planned_slip:.2f}bps")
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
                                    console.print(
                                        f"SKIP COST {sig.symbol} residual={abs(snap.edge_bps):.2f}bps cost={cost:.2f} required={required:.2f}"
                                    )
                                else:
                                    side = OrderSide.LONG if sig.direction > 0 else OrderSide.SHORT
                                    best = await adapter.get_best_price(sig.symbol, side)
                                    detail = testnet_detail[sig.symbol]
                                    limit_price = _cross_limit(best, side, args.ioc_cross_bps, detail)
                                    target_qty = args.target_notional_usdt / best
                                    max_qty = _max_base_qty(detail)
                                    if max_qty is not None:
                                        target_qty = min(target_qty, max_qty)
                                    max_leverage = max(1, int(detail.get("maxLeverage") or 1))
                                    leverage = max_leverage if args.demo_leverage <= 0 else min(max_leverage, args.demo_leverage)
                                    marks: dict[str, float] = {}
                                    submit_start = time.monotonic()
                                    fill = await adapter.open_ioc(
                                        symbol=sig.symbol,
                                        side=side,
                                        price=limit_price,
                                        qty=target_qty,
                                        leverage=leverage,
                                        client_order_id=f"kgv1-{uuid.uuid4().hex}",
                                        timing_marks=marks,
                                    )
                                    remote = await _reconcile_ioc_position(adapter, sig.symbol, side, fill)
                                    if remote is None:
                                        stats.no_fill += 1
                                        console.print(
                                            f"DEMO NO FILL {sig.symbol} requested=${target_qty * best:.0f} "
                                            f"signal_to_submit={(submit_start - signal_mono)*1000:.1f}ms"
                                        )
                                    else:
                                        actual_entry = remote.entry_price or fill.avg_price or best
                                        actual_notional = remote.qty * actual_entry
                                        actual_slip = _entry_slippage_bps(side, best, actual_entry)
                                        fill_ratio = actual_notional / max(args.target_notional_usdt, 1e-12)
                                        if actual_notional < args.min_filled_notional_usdt:
                                            stats.aborts += 1
                                            stats.abort_pnl_usdt += await _abort_position(
                                                adapter, remote, actual_entry, fill.fee_usdt, "below_min_filled_notional"
                                            )
                                        elif actual_slip > args.max_entry_slippage_bps + 1e-9:
                                            stats.aborts += 1
                                            stats.abort_pnl_usdt += await _abort_position(
                                                adapter, remote, actual_entry, fill.fee_usdt, "actual_slippage_exceeded"
                                            )
                                        else:
                                            stats.entries += 1
                                            stats.fills.append(fill_ratio)
                                            stats.notionals.append(actual_notional)
                                            pos = Position(
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
                                            )
                                            post_ms = marks.get("ioc_post_response_ms", 0) - marks.get("ioc_post_start_ms", 0)
                                            confirm_ms = marks.get("ioc_confirmed_ms", 0) - marks.get("ioc_post_start_ms", 0)
                                            console.print(
                                                f"[green]DEMO ENTRY[/green] {sig.symbol} {side.value.upper()} "
                                                f"requested=${args.target_notional_usdt:.0f} filled=${actual_notional:.0f} ({fill_ratio:.1%}) "
                                                f"qty={remote.qty:g} entry={actual_entry:g} fee={fill.fee_usdt:.6f} "
                                                f"live_residual={snap.edge_bps:+.2f}bps live_cost={cost:.2f}bps "
                                                f"demo_slip={actual_slip:.2f}bps signal_to_submit={(submit_start-signal_mono)*1000:.1f}ms "
                                                f"post={post_ms:.1f}ms confirm={confirm_ms:.1f}ms"
                                            )

                if pos is not None:
                    book = mexc.books.get(pos.signal.symbol)
                    snap = models[pos.signal.symbol].snapshot(now_ms=now_ms, threshold_bps=0.0)
                    if book is not None and _valid_snapshot(snap):
                        age_ms = now_ms - pos.entry_ts_ms
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
                            if full_filled + 1e-12 >= pos.remote.qty and full_exit > 0
                            else None
                        )
                        trail = pos.trailing.update(executable_pnl_bps) if executable_pnl_bps is not None else None

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
                            fresh = await adapter.get_position(pos.signal.symbol)
                            if fresh is None:
                                raise MexcWebError(
                                    f"Remote Testnet position disappeared before strategy exit: {pos.signal.symbol}"
                                )
                            pos.remote = fresh
                            exit_fill = await _flatten_position(adapter, fresh, pos.exit_reason)
                            fees = pos.entry_fee_usdt + exit_fill.fee_usdt
                            side = OrderSide.LONG if pos.signal.direction > 0 else OrderSide.SHORT
                            net, price_pct, roe_pct = _trade_pnl(
                                side,
                                pos.entry_demo_price,
                                exit_fill.avg_price,
                                fresh.qty,
                                fresh.leverage,
                                fees,
                            )
                            zero_fee, _, _ = _trade_pnl(
                                side,
                                pos.entry_demo_price,
                                exit_fill.avg_price,
                                fresh.qty,
                                fresh.leverage,
                                0.0,
                            )
                            stats.net_pnl_usdt += net
                            stats.zero_fee_pnl_usdt += zero_fee
                            stats.holds.append(float(age_ms))
                            stats.reasons[pos.exit_reason] = stats.reasons.get(pos.exit_reason, 0) + 1
                            if net > 1e-9:
                                stats.wins += 1
                                stats.gross_win_usdt += net
                            elif net < -1e-9:
                                stats.losses += 1
                                stats.gross_loss_usdt += abs(net)
                            else:
                                stats.flats += 1
                            console.print(
                                f"[{'green' if net > 0 else 'red'}]DEMO EXIT[/] {pos.signal.symbol} {pos.exit_reason} "
                                f"exit={exit_fill.avg_price:g} fees={fees:.6f} NET=${net:+.6f} ZERO_FEE=${zero_fee:+.6f} "
                                f"price={price_pct:+.4f}% roe={roe_pct:+.3f}% hold={age_ms}ms"
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
                            signal_id=f"kgv1-{stats.signals}-{now_ms}",
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
                    stats.signals,
                    stats.entries,
                    stats.expired,
                    stats.no_fill,
                    stats.aborts,
                    stats.closed,
                    round(stats.net_pnl_usdt, 6),
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

        console.print("\n[bold]FINAL REAL TESTNET KNOWN-GOOD REPORT[/bold]")
        console.print(_summary(stats, args.target_closed_trades))


def build_parser() -> argparse.ArgumentParser:
    p = v2.build_parser()
    p.description = "Known-good 372c3b2 arrival-book strategy with real same-symbol MEXC Testnet execution"
    p.add_argument("--target-closed-trades", type=int, default=100)
    p.add_argument("--demo-leverage", type=int, default=0, help="0 = Testnet contract max leverage")
    return p


def main() -> None:
    args = build_parser().parse_args()
    if args.target_closed_trades <= 0:
        raise SystemExit("target closed trades must be positive")
    try:
        asyncio.run(run(args))
    except MexcWebError as exc:
        console.print(f"[red]TESTNET KNOWN-GOOD FAILED:[/red] {exc}")
        raise SystemExit(2) from exc


if __name__ == "__main__":
    main()
