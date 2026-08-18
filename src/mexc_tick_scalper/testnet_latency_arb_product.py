from __future__ import annotations

import argparse
import asyncio
import math
import statistics
import time
import uuid
from collections import defaultdict, deque
from dataclasses import dataclass, field

from rich.console import Console

from .baseline_v1 import apply_baseline_v1
from .demo_hybrid_test import _flatten_position, _load_project_env, _reconcile_ioc_position
from .demo_smoke import _assert_demo_safety
from .demo_tick_test import _trade_pnl
from .execution import OrderSide, PositionSnapshot
from .lead_lag import fetch_binance_usdm_symbols, mexc_to_binance_symbol
from .lead_lag_strategy import LeadLagGate
from .live_lead_lag_shadow import PositiveTrailing
from .live_zero_fee_universe import LiveZeroFeeContract
from .microspread import MicroSpreadModel
from .microspread_feed import EventBinanceBookTickerFeed, EventMexcDepthFeed
from .prelive_latency_diagnostic import _exit_depth_for_qty
from .prelive_measured_rtt_diagnostic import _percentile
from .prelive_persistent_catchup_shadow import directional_move_bps
from .prelive_persistent_ioc_shadow import immediate_roundtrip_cost_bps, virtual_ioc_fill
from .testnet_known_good_risk import (
    _liq_fee_rate_from_detail,
    _mmr_from_detail,
    adverse_roe_pct,
    liquidation_distance_bps,
    theoretical_isolated_liq_price,
)
from .testnet_known_good_v1 import (
    Stats,
    _cross_limit,
    _entry_slippage_bps,
    _event_key,
    _max_base_qty,
    _measure_testnet_private_rtt,
    _summary,
    _testnet_contract_rows,
    _valid_snapshot,
)
from .web_execution import MexcWebError, MexcWebExecutionAdapter, WebExecutionConfig

console = Console()
TESTNET_WS = "wss://futures.testnet.mexc.com/edge"


@dataclass(slots=True)
class LagEpisode:
    started_mono: float
    direction: int
    initial_residual_bps: float


@dataclass(slots=True)
class LagStats:
    completed_ms: deque[float] = field(default_factory=lambda: deque(maxlen=64))
    current: LagEpisode | None = None

    def observe(self, *, residual_bps: float, threshold_bps: float, now: float) -> None:
        direction = 1 if residual_bps > 0 else -1 if residual_bps < 0 else 0
        active = direction != 0 and abs(residual_bps) >= threshold_bps
        if self.current is None:
            if active:
                self.current = LagEpisode(now, direction, abs(residual_bps))
            return
        if not active or direction != self.current.direction:
            self.completed_ms.append((now - self.current.started_mono) * 1000.0)
            self.current = LagEpisode(now, direction, abs(residual_bps)) if active else None

    def median_ms(self) -> float:
        return statistics.median(self.completed_ms) if self.completed_ms else 0.0

    def survival_rate(self, budget_ms: float) -> float:
        if not self.completed_ms:
            return 0.0
        return sum(x >= budget_ms for x in self.completed_ms) / len(self.completed_ms)


@dataclass(slots=True)
class ProductPosition:
    remote: PositionSnapshot
    symbol: str
    direction: int
    entry_price: float
    entry_fee_usdt: float
    entry_notional: float
    entry_testnet_mid: float
    entry_binance_mid: float
    entry_residual_bps: float
    submit_mono: float
    submit_wall_ms: int
    trailing: PositiveTrailing
    theoretical_liq_price: float | None
    exit_reason: str | None = None
    last_risk_poll_mono: float = 0.0


@dataclass(slots=True)
class LatencyModel:
    bootstrap_entry_ms: float
    bootstrap_exit_ms: float
    entry_samples: deque[float] = field(default_factory=lambda: deque(maxlen=32))
    exit_samples: deque[float] = field(default_factory=lambda: deque(maxlen=32))

    @staticmethod
    def _robust(values: deque[float], fallback: float) -> float:
        if not values:
            return fallback
        rows = sorted(values)
        idx = min(len(rows) - 1, math.ceil(0.75 * len(rows)) - 1)
        return max(fallback * 0.5, rows[idx])

    def entry_ms(self) -> float:
        return self._robust(self.entry_samples, self.bootstrap_entry_ms)

    def exit_ms(self) -> float:
        return self._robust(self.exit_samples, self.bootstrap_exit_ms)

    def total_budget_ms(self, safety_ms: float) -> float:
        return self.entry_ms() + self.exit_ms() + max(0.0, safety_ms)


def expected_remaining_edge_bps(*, residual_bps: float, entry_latency_ms: float, median_lifetime_ms: float) -> float:
    """Conservative linear decay proxy for edge remaining when an IOC can reach the follower.

    It is deliberately simple and monotonic: when measured latency approaches the observed
    lag lifetime, expected edge approaches zero and entry is blocked.
    """
    if residual_bps <= 0 or median_lifetime_ms <= 0:
        return 0.0
    retention = max(0.0, 1.0 - entry_latency_ms / median_lifetime_ms)
    return residual_bps * retention


def latency_economics_ok(
    *,
    residual_bps: float,
    roundtrip_cost_bps: float,
    median_lifetime_ms: float,
    survival_rate: float,
    entry_latency_ms: float,
    total_latency_budget_ms: float,
    min_survival_rate: float,
    min_profit_reserve_bps: float,
) -> tuple[bool, float, str]:
    if median_lifetime_ms < total_latency_budget_ms:
        return False, 0.0, "lag_window_shorter_than_entry_plus_exit_latency"
    if survival_rate < min_survival_rate:
        return False, 0.0, "lag_survival_rate_too_low"
    remaining = expected_remaining_edge_bps(
        residual_bps=abs(residual_bps),
        entry_latency_ms=entry_latency_ms,
        median_lifetime_ms=median_lifetime_ms,
    )
    required = roundtrip_cost_bps + min_profit_reserve_bps
    if remaining < required:
        return False, remaining, "remaining_edge_below_cost_plus_profit_reserve"
    return True, remaining, "ok"


async def _discover_testnet_binance_universe(
    adapter: MexcWebExecutionAdapter,
) -> tuple[list[LiveZeroFeeContract], dict[str, dict]]:
    rows = await _testnet_contract_rows(adapter)
    binance = await fetch_binance_usdm_symbols()
    contracts: list[LiveZeroFeeContract] = []
    details: dict[str, dict] = {}
    for symbol, row in rows.items():
        bsymbol = mexc_to_binance_symbol(symbol)
        if bsymbol not in binance:
            continue
        contract_size = float(row.get("contractSize") or 0)
        min_vol = float(row.get("minVol") or 0)
        if contract_size <= 0 or min_vol <= 0:
            continue
        contracts.append(LiveZeroFeeContract(
            mexc_symbol=symbol,
            binance_symbol=bsymbol,
            max_leverage=max(1, int(row.get("maxLeverage") or 1)),
            contract_size=contract_size,
            min_vol=min_vol,
        ))
        details[symbol] = row
        adapter._contract_cache[symbol] = row
    if not contracts:
        raise MexcWebError("No Binance USD-M symbols are executable on MEXC Testnet")
    return contracts, details


async def _flatten_and_pnl(
    adapter: MexcWebExecutionAdapter,
    pos: ProductPosition,
    reason: str,
) -> tuple[float, float, float, float]:
    fresh = await adapter.get_position(pos.symbol)
    if fresh is None:
        raise MexcWebError(f"{reason}: Testnet position disappeared before close")
    t0 = time.monotonic()
    exit_fill = await _flatten_position(adapter, fresh, reason)
    close_ms = (time.monotonic() - t0) * 1000.0
    fees = pos.entry_fee_usdt + exit_fill.fee_usdt
    net, _, roe = _trade_pnl(fresh.side, pos.entry_price, exit_fill.avg_price, fresh.qty, fresh.leverage, fees)
    zero_fee, _, _ = _trade_pnl(fresh.side, pos.entry_price, exit_fill.avg_price, fresh.qty, fresh.leverage, 0.0)
    return net, zero_fee, roe, close_ms


async def run(args: argparse.Namespace) -> None:
    _load_project_env()
    cfg = WebExecutionConfig.demo_from_env(write_enabled=True)
    _assert_demo_safety(cfg)

    async with MexcWebExecutionAdapter(cfg) as adapter:
        if await adapter.get_positions():
            raise MexcWebError("Refusing start: Testnet already has an open position")

        contracts, details = await _discover_testnet_binance_universe(adapter)
        symbols = [c.mexc_symbol for c in contracts]
        by_symbol = {c.mexc_symbol: c for c in contracts}

        rtts = await _measure_testnet_private_rtt(adapter, symbols[0], samples=max(8, args.rtt_warmup_samples + 4))
        private_p75 = _percentile(rtts, .75)
        # No artificial wait before submit. Network latency itself is the latency being arbitraged around.
        latency = LatencyModel(
            bootstrap_entry_ms=max(1.0, private_p75 / 2.0),
            bootstrap_exit_ms=max(1.0, private_p75 / 2.0),
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
        binance_feed = EventBinanceBookTickerFeed(contracts, models, wake)
        testnet_feed = EventMexcDepthFeed(
            symbols, models, wake, depth_limit=args.depth_limit, ws_url=TESTNET_WS
        )
        await binance_feed.start()
        await testnet_feed.start()

        lag = defaultdict(LagStats)
        stats = Stats()
        pos: ProductPosition | None = None
        deadline = time.monotonic() + args.session_seconds
        warmup_until = time.monotonic() + args.warmup_seconds
        last_report: tuple | None = None
        blocked: dict[str, int] = defaultdict(int)

        console.print("[bold cyan]DIRECT BINANCE -> MEXC TESTNET LATENCY-ARB PRODUCT[/bold cyan]")
        console.print(
            "Leader=LIVE Binance USD-M; follower AND execution=MEXC Testnet. "
            "There is NO artificial pre-submit sleep and NO LIVE-MEXC/Testnet market mixing."
        )
        console.print(
            f"Entry requires observed lag window to survive entry+exit latency and expected remaining edge >= "
            f"executable roundtrip cost + {args.min_profit_reserve_bps:.2f}bps reserve."
        )
        console.print(
            f"Initial private RTT p75={private_p75:.1f}ms; bootstrap entry={latency.entry_ms():.1f}ms "
            f"exit={latency.exit_ms():.1f}ms; universe={len(symbols)}"
        )

        try:
            while time.monotonic() < deadline and stats.closed < args.target_closed_trades:
                now = time.monotonic()
                now_ms = int(time.time() * 1000)

                for symbol in symbols:
                    book = testnet_feed.books.get(symbol)
                    if book is None or now_ms - book.recv_ms > args.max_book_age_ms:
                        continue
                    snap = models[symbol].snapshot(now_ms=now_ms, threshold_bps=0.0)
                    if not _valid_snapshot(snap):
                        continue
                    lag[symbol].observe(
                        residual_bps=snap.edge_bps,
                        threshold_bps=args.min_absolute_residual_bps,
                        now=now,
                    )

                if pos is not None:
                    book = testnet_feed.books.get(pos.symbol)
                    snap = models[pos.symbol].snapshot(now_ms=now_ms, threshold_bps=0.0)
                    if book is not None and _valid_snapshot(snap):
                        age_ms = (now - pos.submit_mono) * 1000.0
                        fresh: PositionSnapshot | None = None
                        if now - pos.last_risk_poll_mono >= args.risk_poll_ms / 1000.0:
                            pos.last_risk_poll_mono = now
                            fresh = await adapter.get_position(pos.symbol)
                            if fresh is not None:
                                pos.remote = fresh
                                exit_side = OrderSide.SHORT if fresh.side is OrderSide.LONG else OrderSide.LONG
                                executable = book.bid if exit_side is OrderSide.SHORT else book.ask
                                liq_dist = liquidation_distance_bps(fresh.side, executable, fresh.liquidation_price)
                                roe_now = adverse_roe_pct(fresh.side, pos.entry_price, executable, fresh.leverage)
                                if liq_dist <= args.emergency_liq_distance_bps:
                                    pos.exit_reason = "emergency_liquidation_buffer"
                                elif roe_now <= -abs(args.max_adverse_roe_pct):
                                    pos.exit_reason = "emergency_max_adverse_roe"

                        mid_move = directional_move_bps(pos.direction, pos.entry_testnet_mid, book.mid)
                        leader_move = directional_move_bps(pos.direction, pos.entry_binance_mid, snap.binance_mid)
                        conv = max(args.convergence_bps, abs(pos.entry_residual_bps) * args.convergence_fraction)
                        residual_dir = 1 if snap.edge_bps > 0 else -1 if snap.edge_bps < 0 else 0
                        full_filled, full_exit = _exit_depth_for_qty(
                            book, direction=pos.direction, qty=pos.remote.qty,
                            contract_size=by_symbol[pos.symbol].contract_size,
                        )
                        executable_pnl_bps = None
                        if full_filled + 1e-12 >= pos.remote.qty and full_exit > 0:
                            executable_pnl_bps = directional_move_bps(pos.direction, pos.entry_price, full_exit)
                        trail = pos.trailing.update(executable_pnl_bps) if executable_pnl_bps is not None else None

                        if pos.exit_reason is None and age_ms >= args.min_hold_ms:
                            if mid_move <= -args.mid_adverse_cut_bps:
                                pos.exit_reason = "mid_adverse_cut"
                            elif leader_move <= -args.leader_retrace_exit_bps:
                                pos.exit_reason = "leader_retrace"
                            elif residual_dir == -pos.direction and abs(snap.edge_bps) >= args.reversal_edge_bps:
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
                            net, zero_fee, roe, close_ms = await _flatten_and_pnl(adapter, pos, reason)
                            latency.exit_samples.append(close_ms)
                            stats.net_pnl_usdt += net
                            stats.zero_fee_pnl_usdt += zero_fee
                            stats.holds.append(age_ms)
                            stats.reasons[reason] = stats.reasons.get(reason, 0) + 1
                            if net > 1e-9:
                                stats.wins += 1
                                stats.gross_win_usdt += net
                            elif net < -1e-9:
                                stats.losses += 1
                                stats.gross_loss_usdt += abs(net)
                            else:
                                stats.flats += 1
                            console.print(
                                f"[{'green' if net > 0 else 'red'}]EXIT[/] {pos.symbol} {reason} "
                                f"NET=${net:+.6f} ZERO_FEE=${zero_fee:+.6f} ROE={roe:+.2f}% "
                                f"hold={age_ms:.0f}ms close_e2e={close_ms:.1f}ms"
                            )
                            pos = None

                if now >= warmup_until and pos is None:
                    candidates = []
                    for symbol in symbols:
                        book = testnet_feed.books.get(symbol)
                        if book is None or now_ms - book.recv_ms > args.max_book_age_ms:
                            continue
                        snap = models[symbol].snapshot(now_ms=now_ms, threshold_bps=0.0)
                        if not _valid_snapshot(snap):
                            continue
                        decision = gate.observe(symbol, snap, book.spread_bps, now_ms, event_key=_event_key(models[symbol]))
                        if not decision.ready:
                            continue
                        strength = abs(decision.residual_bps) / max(decision.threshold_bps, 1e-12)
                        if strength < args.min_signal_strength_ratio or abs(decision.residual_bps) < args.min_absolute_residual_bps:
                            continue

                        profile = lag[symbol]
                        if len(profile.completed_ms) < args.min_latency_profile_samples:
                            blocked["profile_warmup"] += 1
                            continue

                        planned = virtual_ioc_fill(
                            book,
                            direction=decision.direction,
                            target_notional_usdt=args.target_notional_usdt,
                            contract_size=by_symbol[symbol].contract_size,
                            cross_bps=args.ioc_cross_bps,
                        )
                        planned_notional = planned.qty * planned.avg_price
                        if planned.qty <= 0 or planned_notional < args.min_filled_notional_usdt:
                            blocked["no_executable_ioc"] += 1
                            continue
                        planned_slip = _entry_slippage_bps(
                            OrderSide.LONG if decision.direction > 0 else OrderSide.SHORT,
                            book.ask if decision.direction > 0 else book.bid,
                            planned.avg_price,
                        )
                        if planned_slip > args.max_entry_slippage_bps + 1e-9:
                            blocked["entry_slippage"] += 1
                            continue
                        cost = immediate_roundtrip_cost_bps(
                            book,
                            direction=decision.direction,
                            entry_price=planned.avg_price,
                            qty=planned.qty,
                            contract_size=by_symbol[symbol].contract_size,
                        )
                        total_budget = latency.total_budget_ms(args.latency_safety_ms)
                        survival = profile.survival_rate(total_budget)
                        ok, remaining, why = latency_economics_ok(
                            residual_bps=decision.residual_bps,
                            roundtrip_cost_bps=cost,
                            median_lifetime_ms=profile.median_ms(),
                            survival_rate=survival,
                            entry_latency_ms=latency.entry_ms(),
                            total_latency_budget_ms=total_budget,
                            min_survival_rate=args.min_latency_survival_rate,
                            min_profit_reserve_bps=args.min_profit_reserve_bps,
                        )
                        if not ok:
                            blocked[why] += 1
                            continue
                        candidates.append((remaining - cost, abs(decision.residual_bps), symbol, decision, snap, book, planned, cost, remaining, survival))

                    if candidates:
                        _, _, symbol, decision, snap, book, planned, cost, remaining, survival = max(candidates)
                        stats.signals += 1
                        side = OrderSide.LONG if decision.direction > 0 else OrderSide.SHORT
                        detail = details[symbol]
                        limit_price = _cross_limit(book.ask if side is OrderSide.LONG else book.bid, side, args.ioc_cross_bps, detail)
                        target_qty = args.target_notional_usdt / (book.ask if side is OrderSide.LONG else book.bid)
                        max_qty = _max_base_qty(detail)
                        if max_qty is not None:
                            target_qty = min(target_qty, max_qty)
                        leverage = min(max(1, int(args.risk_max_leverage)), max(1, int(detail.get("maxLeverage") or 1)))

                        submit_mono = time.monotonic()
                        submit_wall_ms = int(time.time() * 1000)
                        marks: dict[str, float] = {}
                        fill = await adapter.open_ioc(
                            symbol=symbol,
                            side=side,
                            price=limit_price,
                            qty=target_qty,
                            leverage=leverage,
                            client_order_id=f"lat-{uuid.uuid4().hex}",
                            timing_marks=marks,
                        )
                        post_response_ms = marks.get("ioc_post_response_ms", 0.0) - marks.get("ioc_post_start_ms", 0.0)
                        if post_response_ms > 0:
                            latency.entry_samples.append(post_response_ms)
                        remote = await _reconcile_ioc_position(adapter, symbol, side, fill)
                        if remote is None:
                            stats.no_fill += 1
                            console.print(f"NO FILL {symbol} requested=${args.target_notional_usdt:.0f}")
                            continue

                        entry = remote.entry_price or fill.avg_price or limit_price
                        notional = remote.qty * entry
                        fill_ratio = notional / max(args.target_notional_usdt, 1e-12)
                        actual_slip = _entry_slippage_bps(side, book.ask if side is OrderSide.LONG else book.bid, entry)
                        mmr = _mmr_from_detail(detail)
                        liq_fee = _liq_fee_rate_from_detail(detail)
                        theoretical_liq = theoretical_isolated_liq_price(side, entry, remote.leverage, mmr, liq_fee)
                        executable = book.bid if side is OrderSide.LONG else book.ask
                        liq_dist = liquidation_distance_bps(side, executable, remote.liquidation_price)

                        if notional < args.min_filled_notional_usdt or actual_slip > args.max_entry_slippage_bps + 1e-9 or liq_dist < args.min_liq_distance_bps:
                            tmp = ProductPosition(remote, symbol, decision.direction, entry, fill.fee_usdt, notional, book.mid,
                                                  snap.binance_mid, decision.residual_bps, submit_mono, submit_wall_ms,
                                                  PositiveTrailing(distance_bps=max(args.trailing_distance_bps, book.spread_bps)),
                                                  theoretical_liq)
                            net, _, _, close_ms = await _flatten_and_pnl(adapter, tmp, "entry_safety_abort")
                            latency.exit_samples.append(close_ms)
                            stats.aborts += 1
                            stats.abort_pnl_usdt += net
                            continue

                        stats.entries += 1
                        stats.fills.append(fill_ratio)
                        stats.notionals.append(notional)
                        pos = ProductPosition(
                            remote=remote,
                            symbol=symbol,
                            direction=decision.direction,
                            entry_price=entry,
                            entry_fee_usdt=fill.fee_usdt,
                            entry_notional=notional,
                            entry_testnet_mid=book.mid,
                            entry_binance_mid=snap.binance_mid,
                            entry_residual_bps=decision.residual_bps,
                            submit_mono=submit_mono,
                            submit_wall_ms=submit_wall_ms,
                            trailing=PositiveTrailing(distance_bps=max(args.trailing_distance_bps, book.spread_bps)),
                            theoretical_liq_price=theoretical_liq,
                            last_risk_poll_mono=time.monotonic(),
                        )
                        console.print(
                            f"[green]ENTRY[/green] {symbol} {side.value.upper()} requested=${args.target_notional_usdt:.0f} "
                            f"filled=${notional:.0f} ({fill_ratio:.1%}) residual={decision.residual_bps:+.2f}bps "
                            f"expected_remaining={remaining:.2f}bps cost={cost:.2f}bps reserve={remaining-cost:.2f}bps "
                            f"lag_med={lag[symbol].median_ms():.0f}ms survive={survival:.0%} "
                            f"entry_e2e={post_response_ms:.1f}ms liq={liq_dist:.0f}bps"
                        )

                report = (stats.signals, stats.entries, stats.closed, stats.no_fill, stats.aborts, round(stats.net_pnl_usdt, 6))
                if report != last_report:
                    block_text = ",".join(f"{k}:{v}" for k, v in sorted(blocked.items())) or "-"
                    console.print(
                        "STATE " + _summary(stats, args.target_closed_trades) +
                        f" latency_entry={latency.entry_ms():.1f}ms latency_exit={latency.exit_ms():.1f}ms blocks={block_text}"
                    )
                    last_report = report

                wake.clear()
                try:
                    await asyncio.wait_for(wake.wait(), timeout=0.01)
                except TimeoutError:
                    pass
        finally:
            if pos is not None:
                try:
                    fresh = await adapter.get_position(pos.symbol)
                    if fresh is not None:
                        await _flatten_position(adapter, fresh, "shutdown_cleanup")
                except Exception as exc:
                    console.print(f"[red]CLEANUP FAILED[/red] {exc}")
            await binance_feed.close()
            await testnet_feed.close()

        console.print("\n[bold]FINAL TESTNET LATENCY-ARB PRODUCT REPORT[/bold]")
        console.print(_summary(stats, args.target_closed_trades))


def build_parser() -> argparse.ArgumentParser:
    # Reuse the frozen strategy defaults, then add product-level latency economics/risk settings.
    from . import prelive_persistent_ioc_shadow_v2 as v2
    p = v2.build_parser()
    p.description = "Direct Binance->MEXC Testnet latency arbitrage with latency-survival and profit-reserve gating"
    p.add_argument("--target-closed-trades", type=int, default=100)
    p.add_argument("--risk-max-leverage", type=int, default=10)
    p.add_argument("--min-liq-distance-bps", type=float, default=500.0)
    p.add_argument("--emergency-liq-distance-bps", type=float, default=300.0)
    p.add_argument("--max-adverse-roe-pct", type=float, default=8.0)
    p.add_argument("--risk-poll-ms", type=float, default=100.0)
    p.add_argument("--min-latency-profile-samples", type=int, default=4)
    p.add_argument("--min-latency-survival-rate", type=float, default=0.60)
    p.add_argument("--latency-safety-ms", type=float, default=50.0)
    p.add_argument("--min-profit-reserve-bps", type=float, default=2.0)
    return p


def main() -> None:
    args = build_parser().parse_args()
    apply_baseline_v1(args)
    if args.target_closed_trades <= 0:
        raise SystemExit("target closed trades must be positive")
    try:
        asyncio.run(run(args))
    except MexcWebError as exc:
        console.print(f"[red]LATENCY-ARB PRODUCT FAILED:[/red] {exc}")
        raise SystemExit(2) from exc


if __name__ == "__main__":
    main()
