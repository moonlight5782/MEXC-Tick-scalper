from __future__ import annotations

import argparse
import asyncio
import math
import statistics
import time
import uuid
from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR

from rich.console import Console

from .baseline_v1 import apply_baseline_v1
from .demo_baseline_v1_mirror import _usable_demo_symbols
from .demo_baseline_v1_signal_test import _live_contracts
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
from .prelive_persistent_ioc_shadow_v2 import _event_key, _valid_snapshot, entry_slippage_bps, executable_edge_ok
from .web_execution import MexcWebError, MexcWebExecutionAdapter, WebExecutionConfig

console = Console()


@dataclass(slots=True)
class Pending:
    signal: Signal
    execute_at: float


@dataclass(slots=True)
class DemoPosition:
    signal: Signal
    remote: PositionSnapshot
    entry_ts_ms: int
    live_entry_mid: float
    live_entry_binance: float
    live_entry_residual_bps: float
    demo_entry_price: float
    entry_fee_usdt: float
    trailing: PositiveTrailing
    exit_reason: str | None = None


def _cross_limit(best: float, side: OrderSide, cross_bps: float, price_unit: float) -> float:
    if best <= 0:
        raise MexcWebError("invalid Testnet best price")
    factor = Decimal("1") + (Decimal(str(cross_bps)) / Decimal("10000"))
    if side is OrderSide.SHORT:
        factor = Decimal("1") - (Decimal(str(cross_bps)) / Decimal("10000"))
    raw = Decimal(str(best)) * factor
    unit = Decimal(str(price_unit)) if price_unit > 0 else Decimal("0")
    if unit <= 0:
        return float(raw)
    rounding = ROUND_CEILING if side is OrderSide.LONG else ROUND_FLOOR
    return float((raw / unit).to_integral_value(rounding=rounding) * unit)


async def _demo_universe(adapter: MexcWebExecutionAdapter):
    demo_symbols = await _usable_demo_symbols(adapter)
    contracts = await _live_contracts(sorted(demo_symbols))
    if not contracts:
        raise MexcWebError("No exact symbol is available on MEXC Testnet + LIVE MEXC + Binance USD-M")
    return contracts


async def run(args: argparse.Namespace) -> None:
    _load_project_env()
    cfg = WebExecutionConfig.demo_from_env(write_enabled=True)
    _assert_demo_safety(cfg)

    async with MexcWebExecutionAdapter(cfg) as adapter:
        contracts = await _demo_universe(adapter)
        symbols = [c.mexc_symbol for c in contracts]
        by_symbol = {c.mexc_symbol: c for c in contracts}

        # Refuse to start on top of an existing Demo position.
        existing = await adapter.get_positions()
        if existing:
            names = ",".join(f"{p.symbol}:{p.side.value}:{p.qty:g}" for p in existing)
            raise MexcWebError(f"Demo account already has open position(s): {names}")

        detail = {s: await adapter.get_contract_detail(s) for s in symbols}
        console.print("[bold cyan]FROZEN BASELINE V1 -> REAL MEXC TESTNET[/bold cyan]")
        console.print(
            "One runner only: no paper child, no stdout mirror, no proxy symbol. "
            "Signals use LIVE Binance + LIVE MEXC; every accepted ENTRY opens the SAME symbol on MEXC Testnet."
        )
        console.print(
            f"SAME-SYMBOL TESTNET UNIVERSE {len(symbols)} pair(s): " + ",".join(symbols)
        )
        console.print(
            "BASELINE_V1 signal/RTT/retention/arrival-book IOC/slippage/cost/trailing/exit thresholds are frozen. "
            "Only production pair fee/profile eligibility is adapted to Testnet availability; actual Testnet entry+exit fees are deducted."
        )

        rtts = await measure_live_private_rtt(
            samples=args.rtt_samples,
            warmup_samples=args.rtt_warmup_samples,
            interval_ms=args.rtt_interval_ms,
        )
        rtt_ms = statistics.median(rtts)
        console.print(f"Measured private RTT median={rtt_ms:.1f}ms p95={_percentile(rtts, .95):.1f}ms")

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
        pos: DemoPosition | None = None
        signals = entries = expired = nofill = wins = losses = flats = closed = 0
        net_pnl = gross_win = gross_loss = 0.0
        deadline = time.monotonic() + args.session_seconds
        warmup_until = time.monotonic() + args.warmup_seconds

        try:
            while time.monotonic() < deadline and signals < args.max_signals and closed < args.target_closed_trades:
                now = time.monotonic()
                now_ms = int(time.time() * 1000)

                if pending is not None and pos is None and now >= pending.execute_at:
                    sig = pending.signal
                    pending = None
                    book = mexc.books.get(sig.symbol)
                    snap = models[sig.symbol].snapshot(now_ms=now_ms, threshold_bps=0.0)
                    if book is None or now_ms - book.recv_ms > args.max_book_age_ms or not _valid_snapshot(snap):
                        expired += 1
                    else:
                        ok, why, residual_ret, impulse_ret = delayed_catchup_entry_ok(
                            signal=sig,
                            current_residual_bps=snap.edge_bps,
                            current_binance_price=snap.binance_mid,
                            current_spread_bps=book.spread_bps,
                            min_residual_retention=args.min_residual_retention,
                            min_impulse_retention=args.min_impulse_retention,
                            min_remaining_edge_bps=args.min_absolute_residual_bps,
                            min_edge_after_spread_bps=args.min_edge_after_spread_bps,
                        )
                        if not ok:
                            expired += 1
                            console.print(
                                f"EXPIRED {sig.symbol} reason={why} residual_ret={residual_ret:.1%} impulse_ret={impulse_ret:.1%}"
                            )
                        else:
                            contract = by_symbol[sig.symbol]
                            planned = virtual_ioc_fill(
                                book,
                                direction=sig.direction,
                                target_notional_usdt=args.target_notional_usdt,
                                contract_size=contract.contract_size,
                                cross_bps=args.ioc_cross_bps,
                            )
                            planned_notional = planned.qty * planned.avg_price
                            planned_slip = entry_slippage_bps(sig.direction, book, planned.avg_price)
                            if planned.qty <= 0 or planned_notional < args.min_filled_notional_usdt:
                                nofill += 1
                            elif planned_slip > args.max_entry_slippage_bps + 1e-9:
                                expired += 1
                                console.print(f"SKIP SLIP {sig.symbol} avg_slip={planned_slip:.2f}bps")
                            else:
                                cost = immediate_roundtrip_cost_bps(
                                    book,
                                    direction=sig.direction,
                                    entry_price=planned.avg_price,
                                    qty=planned.qty,
                                    contract_size=contract.contract_size,
                                )
                                edge_ok, required = executable_edge_ok(
                                    snap.edge_bps,
                                    cost,
                                    args.min_executable_net_edge_bps,
                                    args.min_edge_to_cost_ratio,
                                )
                                if not edge_ok:
                                    expired += 1
                                    console.print(
                                        f"SKIP COST {sig.symbol} residual={abs(snap.edge_bps):.2f}bps cost={cost:.2f} required={required:.2f}"
                                    )
                                else:
                                    side = OrderSide.LONG if sig.direction > 0 else OrderSide.SHORT
                                    best = await adapter.get_best_price(sig.symbol, side)
                                    d = detail[sig.symbol]
                                    price = _cross_limit(best, side, args.ioc_cross_bps, float(d.get("priceUnit") or 0))
                                    max_lev = max(1, int(d.get("maxLeverage") or 1))
                                    leverage = max_lev if args.demo_leverage <= 0 else min(max_lev, args.demo_leverage)
                                    qty = args.target_notional_usdt / best
                                    marks: dict[str, float] = {}
                                    fill = await adapter.open_ioc(
                                        symbol=sig.symbol,
                                        side=side,
                                        price=price,
                                        qty=qty,
                                        leverage=leverage,
                                        client_order_id=f"bv1-{uuid.uuid4().hex}",
                                        timing_marks=marks,
                                    )
                                    remote = await _reconcile_ioc_position(adapter, sig.symbol, side, fill)
                                    if remote is None:
                                        nofill += 1
                                        console.print(f"DEMO NO FILL {sig.symbol} requested=${args.target_notional_usdt:.0f}")
                                    else:
                                        actual_entry = remote.entry_price or fill.avg_price or best
                                        actual_notional = remote.qty * actual_entry
                                        entries += 1
                                        pos = DemoPosition(
                                            signal=sig,
                                            remote=remote,
                                            entry_ts_ms=int(time.time() * 1000),
                                            live_entry_mid=book.mid,
                                            live_entry_binance=snap.binance_mid,
                                            live_entry_residual_bps=snap.edge_bps,
                                            demo_entry_price=actual_entry,
                                            entry_fee_usdt=fill.fee_usdt,
                                            trailing=PositiveTrailing(distance_bps=max(args.trailing_distance_bps, book.spread_bps)),
                                        )
                                        post_ms = marks.get("ioc_post_response_ms", 0) - marks.get("ioc_post_start_ms", 0)
                                        confirm_ms = marks.get("ioc_confirmed_ms", 0) - marks.get("ioc_post_start_ms", 0)
                                        console.print(
                                            f"[green]DEMO ENTRY[/green] {sig.symbol} {side.value.upper()} qty={remote.qty:g} "
                                            f"entry={actual_entry:g} notional=${actual_notional:.0f} fee={fill.fee_usdt:.6f} "
                                            f"post={post_ms:.1f}ms confirm={confirm_ms:.1f}ms"
                                        )

                if pos is not None:
                    book = mexc.books.get(pos.signal.symbol)
                    snap = models[pos.signal.symbol].snapshot(now_ms=now_ms, threshold_bps=0.0)
                    if book is not None and _valid_snapshot(snap):
                        age_ms = now_ms - pos.entry_ts_ms
                        mid_move = directional_move_bps(pos.signal.direction, pos.live_entry_mid, book.mid)
                        leader_move = directional_move_bps(pos.signal.direction, pos.live_entry_binance, snap.binance_mid)
                        conv = max(args.convergence_bps, abs(pos.live_entry_residual_bps) * args.convergence_fraction)
                        residual_dir = 1 if snap.edge_bps > 0 else -1 if snap.edge_bps < 0 else 0
                        full_qty, full_exit = _exit_depth_for_qty(
                            book,
                            direction=pos.signal.direction,
                            qty=pos.remote.qty,
                            contract_size=by_symbol[pos.signal.symbol].contract_size,
                        )
                        executable_pnl_bps = (
                            _signed_move_bps(pos.signal.direction, pos.live_entry_mid, full_exit)
                            if full_qty + 1e-12 >= pos.remote.qty and full_exit > 0 else None
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
                            exit_fill = await _flatten_position(adapter, pos.remote, pos.exit_reason)
                            side = OrderSide.LONG if pos.signal.direction > 0 else OrderSide.SHORT
                            fees = pos.entry_fee_usdt + exit_fill.fee_usdt
                            trade_pnl, price_return_pct, roe_pct = _trade_pnl(
                                side,
                                pos.demo_entry_price,
                                exit_fill.avg_price,
                                pos.remote.qty,
                                pos.remote.leverage,
                                fees,
                            )
                            net_pnl += trade_pnl
                            if trade_pnl > 1e-9:
                                wins += 1; gross_win += trade_pnl
                            elif trade_pnl < -1e-9:
                                losses += 1; gross_loss += abs(trade_pnl)
                            else:
                                flats += 1
                            closed += 1
                            console.print(
                                f"[{'green' if trade_pnl > 0 else 'red'}]DEMO EXIT[/] {pos.signal.symbol} {pos.exit_reason} "
                                f"exit={exit_fill.avg_price:g} fees={fees:.6f} net=${trade_pnl:+.6f} "
                                f"price={price_return_pct:+.4f}% roe={roe_pct:+.3f}% hold={age_ms}ms"
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
                        candidates.append((abs(decision.residual_bps), strength, decision.leader_advantage_bps, symbol, decision, snap, book))

                    if candidates:
                        _, strength, _, symbol, decision, snap, book = max(candidates, key=lambda row: (row[0], row[1], row[2]))
                        signals += 1
                        sig = Signal(
                            signal_id=f"demo-bv1-{signals}-{now_ms}",
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
                        pending = Pending(sig, time.monotonic() + rtt_ms / 1000.0)
                        console.print(
                            f"SIGNAL #{signals} {symbol} {'LONG' if decision.direction > 0 else 'SHORT'} "
                            f"residual={decision.residual_bps:+.2f}bps strength={strength:.2f}x live_spread={book.spread_bps:.2f}bps"
                        )

                if signals or entries or expired or nofill or closed:
                    wr = wins / closed * 100 if closed else 0.0
                    pf = gross_win / gross_loss if gross_loss > 0 else (math.inf if gross_win > 0 else 0.0)
                    pf_txt = "inf" if math.isinf(pf) else f"{pf:.3f}"
                    console.print(
                        f"STATE signals={signals} entries={entries} expired={expired} nofill={nofill} "
                        f"closed={closed}/{args.target_closed_trades} W/L/F={wins}/{losses}/{flats} "
                        f"WR={wr:.1f}% PF={pf_txt} NET_AFTER_DEMO_FEES={net_pnl:+.6f}USDT"
                    )

                wake.clear()
                try:
                    await asyncio.wait_for(wake.wait(), timeout=0.02)
                except TimeoutError:
                    pass
        finally:
            if pos is not None:
                try:
                    await _flatten_position(adapter, pos.remote, "shutdown_cleanup")
                except Exception as exc:
                    console.print(f"[red]CLEANUP FAILED[/red] {pos.signal.symbol}: {exc}")
            await binance.close()
            await mexc.close()

        wr = wins / closed * 100 if closed else 0.0
        pf = gross_win / gross_loss if gross_loss > 0 else (math.inf if gross_win > 0 else 0.0)
        pf_txt = "inf" if math.isinf(pf) else f"{pf:.3f}"
        console.print(
            f"[bold]FINAL REAL TESTNET BASELINE_V1 REPORT[/bold] closed={closed} signals={signals} entries={entries} "
            f"expired={expired} nofill={nofill} W/L/F={wins}/{losses}/{flats} WR={wr:.1f}% PF={pf_txt} "
            f"NET_AFTER_DEMO_FEES={net_pnl:+.6f}USDT"
        )


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Frozen BASELINE_V1 with real same-symbol MEXC Testnet execution")
    p.add_argument("--target-closed-trades", type=int, default=100)
    p.add_argument("--session-seconds", type=float, default=86400.0)
    p.add_argument("--max-signals", type=int, default=3000)
    p.add_argument("--demo-leverage", type=int, default=0, help="0 = Testnet contract maximum")

    # Names populated by apply_baseline_v1; defaults exist only so argparse Namespace is explicit.
    for name, value in {
        "target_notional_usdt": 10000.0, "pair_min_signals": 4, "pair_min_median_lifetime_ms": 300.0,
        "pair_min_survival_rate": 0.50, "pair_min_strength_ratio": 1.50, "micro_horizon_ms": 100,
        "baseline_seconds": 8.0, "baseline_exclusion_ms": 1000, "noise_window_ms": 8000,
        "residual_noise_multiplier": 3.0, "binance_noise_multiplier": 1.5, "min_edge_bps": 2.0,
        "min_net_edge_bps": 0.5, "edge_to_spread_ratio": 1.2, "min_binance_move_bps": 1.0,
        "min_leader_advantage_bps": 1.0, "min_lead_ratio": 1.35, "confirm_updates": 2, "confirm_ms": 15,
        "rearm_fraction": 0.35, "min_signal_strength_ratio": 3.0, "min_absolute_residual_bps": 8.0,
        "min_residual_retention": 0.60, "min_impulse_retention": 0.75, "min_edge_after_spread_bps": 2.0,
        "ioc_cross_bps": 1.0, "max_entry_slippage_bps": 1.0, "min_filled_notional_usdt": 50.0,
        "min_executable_net_edge_bps": 2.0, "min_edge_to_cost_ratio": 1.50, "max_binance_age_ms": 300.0,
        "max_mexc_age_ms": 2000.0, "max_book_age_ms": 750.0, "warmup_seconds": 10.0, "depth_limit": 20,
        "min_hold_ms": 50, "max_hold_ms": 15000, "no_progress_ms": 3000, "min_progress_bps": 0.5,
        "convergence_bps": 0.25, "convergence_fraction": 0.25, "min_catchup_bps": 1.0,
        "leader_retrace_exit_bps": 1.5, "reversal_edge_bps": 0.75, "mid_adverse_cut_bps": 3.0,
        "trailing_distance_bps": 1.5, "rtt_samples": 40, "rtt_warmup_samples": 3, "rtt_interval_ms": 100.0,
    }.items():
        setattr(p, f"_baseline_default_{name}", value)
    return p


def main() -> None:
    args = build_parser().parse_args()
    apply_baseline_v1(args)
    if args.target_closed_trades <= 0 or args.session_seconds <= 0 or args.max_signals <= 0:
        raise SystemExit("trade/session/signal limits must be positive")
    try:
        asyncio.run(run(args))
    except MexcWebError as exc:
        console.print(f"[red]REAL TESTNET BASELINE_V1 FAILED:[/red] {exc}")
        raise SystemExit(2) from exc


if __name__ == "__main__":
    main()
