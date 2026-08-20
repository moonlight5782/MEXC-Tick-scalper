from __future__ import annotations

import asyncio
import csv
import math
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv
from rich.console import Console

from . import auto_discovery_shadow as auto
from . import auto_discovery_testnet as common
from .execution import OrderFill, OrderSide, PositionSnapshot
from .lead_lag import fetch_binance_usdm_symbols, mexc_to_binance_symbol
from .lead_lag_strategy import LeadLagGate
from .live_lead_lag_shadow import PositiveTrailing
from .live_production_runner import _close_position_fully, _resolve_remote_position, _signed_move_bps
from .live_zero_fee_universe import LIVE_REST, LIVE_WS, LiveZeroFeeContract
from .market import MexcPublicMarket
from .microspread import MicroSpreadModel
from .microspread_feed import EventBinanceBookTickerFeed, EventMexcDepthFeed
from .prelive_persistent_catchup_shadow import Signal, directional_move_bps
from .prelive_persistent_ioc_shadow import immediate_roundtrip_cost_bps, virtual_ioc_fill
from .prelive_persistent_ioc_shadow_v2 import _event_key, _valid_snapshot, entry_slippage_bps, executable_edge_ok
from .web_execution import DEMO_HOST, MexcWebError, MexcWebExecutionAdapter, WebExecutionConfig

console = Console()
SYMBOL = "XRP_USDT"


@dataclass(slots=True)
class Position:
    signal: Signal
    remote: PositionSnapshot
    entry_fill: OrderFill
    signal_ms: int
    entry_ms: int
    entry_live_mid: float
    entry_binance: float
    entry_residual_bps: float
    demo_entry_best: float
    entry_price: float
    requested_notional: float
    filled_notional: float
    trailing: PositiveTrailing
    runner_armed: bool = False
    mfe_bps: float = 0.0
    mae_bps: float = 0.0
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
    gross_pnl_usdt: float = 0.0
    demo_fees_usdt: float = 0.0
    demo_net_pnl_usdt: float = 0.0
    gross_wins: float = 0.0
    gross_losses: float = 0.0
    fills: list[float] = field(default_factory=list)
    holds: list[float] = field(default_factory=list)
    signal_to_fill: list[float] = field(default_factory=list)
    exits: dict[str, int] = field(default_factory=dict)

    @property
    def pf(self) -> float:
        if self.gross_losses <= 0:
            return math.inf if self.gross_wins > 0 else 0.0
        return self.gross_wins / self.gross_losses


BANK = common.Bank()


def _load_env() -> None:
    root = Path(__file__).resolve().parents[2]
    load_dotenv(root / ".env", override=False)


async def _live_xrp_contract() -> LiveZeroFeeContract:
    binance_symbol = mexc_to_binance_symbol(SYMBOL)
    if binance_symbol not in await fetch_binance_usdm_symbols():
        raise RuntimeError(f"{SYMBOL} has no Binance USD-M counterpart {binance_symbol}")
    market = MexcPublicMarket(LIVE_REST, LIVE_WS)
    rows = await market.contracts()
    row = next((r for r in rows if str(r.get("symbol") or "").upper() == SYMBOL), None)
    if row is None:
        raise RuntimeError(f"{SYMBOL} is not listed on LIVE MEXC Futures")
    return LiveZeroFeeContract(
        mexc_symbol=SYMBOL,
        binance_symbol=binance_symbol,
        max_leverage=int(row.get("maxLeverage") or 1),
        contract_size=float(row.get("contractSize") or 0),
        min_vol=float(row.get("minVol") or 0),
        maintenance_margin_rate=float(row.get("maintenanceMarginRate") or 0),
        initial_margin_rate=float(row.get("initialMarginRate") or 0),
        risk_base_vol=float(row.get("riskBaseVol") or 0),
        risk_incr_vol=float(row.get("riskIncrVol") or 0),
        risk_incr_mmr=float(row.get("riskIncrMmr") or 0),
        risk_level_limit=max(1, int(row.get("riskLevelLimit") or 1)),
        risk_limit_type=str(row.get("riskLimitType") or "BY_VOLUME").upper(),
    )


def _summary(s: Stats) -> str:
    closed = s.wins + s.losses + s.flats
    wr = s.wins / closed * 100 if closed else 0.0
    pf = "inf" if math.isinf(s.pf) else f"{s.pf:.3f}"
    med = lambda xs: sorted(xs)[len(xs)//2] if xs else 0.0
    exits = ",".join(f"{k}:{v}" for k, v in sorted(s.exits.items())) or "-"
    return (
        f"signals={s.signals} entries={s.entries} expired={s.expired} nofill={s.nofill} "
        f"W/L/F={s.wins}/{s.losses}/{s.flats} WR={wr:.1f}% PF_GROSS={pf} "
        f"gross={s.gross_pnl_usdt:+.4f}USDT demo_fees=${s.demo_fees_usdt:.4f} "
        f"demo_net={s.demo_net_pnl_usdt:+.4f}USDT logical_bank=${BANK.balance_usdt:.2f} "
        f"fill_med={med(s.fills)*100:.1f}% hold_med={med(s.holds):.0f}ms "
        f"signal_to_fill_med={med(s.signal_to_fill):.0f}ms exits={exits}"
    )


def _append(path: Path, row: dict[str, object]) -> None:
    fields = [
        "signal_ms","entry_ms","exit_ms","symbol","direction","requested_notional_usdt",
        "filled_notional_usdt","fill_ratio","leverage","actual_margin_usdt","demo_entry_best",
        "entry_price","exit_price","entry_slippage_bps","gross_pnl_bps","gross_pnl_usdt",
        "entry_fee_usdt","exit_fee_usdt","demo_fees_usdt","demo_net_pnl_usdt","gross_roe_pct",
        "mfe_bps","mae_bps","hold_ms","signal_to_fill_ms","exit_reason","runner_armed",
        "liquidation_price","entry_order_id","exit_order_id",
    ]
    exists = path.exists()
    with path.open("a", newline="", encoding="utf-8") as h:
        w = csv.DictWriter(h, fieldnames=fields)
        if not exists:
            w.writeheader()
        w.writerow({k: row.get(k, "") for k in fields})


async def _close(
    adapter: MexcWebExecutionAdapter,
    pos: Position,
    stats: Stats,
    output: Path,
    reason: str,
) -> None:
    started = time.time_ns() / 1_000_000
    fill = await _close_position_fully(adapter, pos.remote)
    done = time.time_ns() / 1_000_000
    exit_price = float(fill.avg_price or pos.entry_price)
    gross = pos.signal.direction * (exit_price - pos.entry_price) * pos.remote.qty
    fees = float(pos.entry_fill.fee_usdt) + float(fill.fee_usdt)
    net = gross - fees
    gross_bps = gross / max(pos.filled_notional, 1e-12) * 10_000
    margin = pos.filled_notional / max(float(pos.remote.leverage), 1.0)
    gross_roe = gross / max(margin, 1e-12) * 100
    hold = max(0, int(done) - pos.entry_ms)

    before = BANK.balance_usdt
    BANK.balance_usdt = max(0.0, BANK.balance_usdt + gross)
    stats.gross_pnl_usdt += gross
    stats.demo_fees_usdt += fees
    stats.demo_net_pnl_usdt += net
    stats.holds.append(float(hold))
    stats.exits[reason] = stats.exits.get(reason, 0) + 1
    if gross > 1e-9:
        stats.wins += 1
        stats.gross_wins += gross
    elif gross < -1e-9:
        stats.losses += 1
        stats.gross_losses += abs(gross)
    else:
        stats.flats += 1

    entry_slip = max(0.0, _signed_move_bps(pos.signal.direction, pos.demo_entry_best, pos.entry_price))
    _append(output, {
        "signal_ms": pos.signal_ms, "entry_ms": pos.entry_ms, "exit_ms": int(done), "symbol": SYMBOL,
        "direction": "LONG" if pos.signal.direction > 0 else "SHORT",
        "requested_notional_usdt": pos.requested_notional, "filled_notional_usdt": pos.filled_notional,
        "fill_ratio": pos.filled_notional / max(pos.requested_notional, 1e-12), "leverage": pos.remote.leverage,
        "actual_margin_usdt": margin, "demo_entry_best": pos.demo_entry_best, "entry_price": pos.entry_price,
        "exit_price": exit_price, "entry_slippage_bps": entry_slip, "gross_pnl_bps": gross_bps,
        "gross_pnl_usdt": gross, "entry_fee_usdt": pos.entry_fill.fee_usdt, "exit_fee_usdt": fill.fee_usdt,
        "demo_fees_usdt": fees, "demo_net_pnl_usdt": net, "gross_roe_pct": gross_roe,
        "mfe_bps": pos.mfe_bps, "mae_bps": pos.mae_bps, "hold_ms": hold,
        "signal_to_fill_ms": pos.entry_ms-pos.signal_ms, "exit_reason": reason, "runner_armed": pos.runner_armed,
        "liquidation_price": pos.remote.liquidation_price or "", "entry_order_id": pos.entry_fill.order_id,
        "exit_order_id": fill.order_id,
    })
    console.print(
        f"[{'green' if gross > 0 else 'red'}]TESTNET EXIT[/] {SYMBOL} reason={reason} "
        f"GROSS={gross_bps:+.2f}bps ${gross:+.2f} gross_ROE={gross_roe:+.1f}% "
        f"DEMO_FEES=${fees:.2f} DEMO_NET=${net:+.2f} exit_submit_to_fill={done-started:.1f}ms "
        f"logical_bank=${before:.2f}->${BANK.balance_usdt:.2f}"
    )


async def run(args) -> None:
    _load_env()
    auto._apply_immediate_exit_policy(args)
    BANK.balance_usdt = auto.START_BANK_USDT
    output = Path(args.testnet_output)
    if output.exists() and not args.append_output:
        output.unlink()

    live_contract = await _live_xrp_contract()
    cfg = WebExecutionConfig.demo_from_env(write_enabled=True)
    common._assert_demo_write_config(cfg)

    stats = Stats()
    wake = asyncio.Event()
    pos: Position | None = None
    async with MexcWebExecutionAdapter(cfg) as adapter:
        await adapter.probe()
        existing = await adapter.get_positions()
        if existing:
            raise MexcWebError("Testnet account already has open Futures positions; close them before XRP plumbing test")
        detail = await adapter.get_contract_detail(SYMBOL)
        if float(detail.get("contractSize") or 0) <= 0 or float(detail.get("priceUnit") or 0) <= 0:
            raise MexcWebError("XRP_USDT Testnet contract metadata is invalid")
        leverage = common._effective_leverage(live_contract.max_leverage, detail)

        model = MicroSpreadModel(
            horizon_ms=args.micro_horizon_ms, baseline_seconds=args.baseline_seconds,
            baseline_exclusion_ms=args.baseline_exclusion_ms, min_edge_bps=0.0, min_binance_move_bps=0.0,
            max_binance_age_ms=args.max_binance_age_ms, max_mexc_age_ms=args.max_mexc_age_ms,
        )
        models = {SYMBOL: model}
        gate = LeadLagGate(
            noise_window_ms=args.noise_window_ms, residual_noise_multiplier=args.residual_noise_multiplier,
            binance_noise_multiplier=args.binance_noise_multiplier, min_edge_bps=args.min_edge_bps,
            min_net_edge_bps=args.min_net_edge_bps, spread_ratio=args.edge_to_spread_ratio,
            min_binance_move_bps=args.min_binance_move_bps,
            min_leader_advantage_bps=args.min_leader_advantage_bps, min_lead_ratio=args.min_lead_ratio,
            confirm_updates=args.confirm_updates, confirm_ms=args.confirm_ms, rearm_fraction=args.rearm_fraction,
        )
        binance = EventBinanceBookTickerFeed([live_contract], models, wake)
        mexc = EventMexcDepthFeed([SYMBOL], models, wake, depth_limit=args.depth_limit)
        await binance.start(); await mexc.start()

        console.print("[bold yellow]FIXED XRP TESTNET PLUMBING[/bold yellow]")
        console.print("LIVE Binance/MEXC are signal-only. Demo prices are execution/slippage/PnL/trailing-only.")
        console.print("Logical bank uses GROSS zero-fee PnL; real Demo fees and Demo net are reported separately.")
        console.print(f"XRP leverage={leverage}x; execution hard-locked to {DEMO_HOST}")

        warmup_until = time.monotonic() + args.warmup_seconds
        deadline = time.monotonic() + args.session_seconds
        last_report = None
        try:
            while time.monotonic() < deadline and stats.signals < args.max_signals and (stats.wins+stats.losses+stats.flats) < args.target_closed_trades:
                now = time.monotonic(); now_ms = int(time.time()*1000)

                if pos is None and now >= warmup_until and BANK.balance_usdt > BANK.stop_balance:
                    book = mexc.books.get(SYMBOL)
                    if book is not None and now_ms-book.recv_ms <= args.max_book_age_ms:
                        snap = model.snapshot(now_ms=now_ms, threshold_bps=0.0)
                        if _valid_snapshot(snap):
                            d = gate.observe(SYMBOL, snap, book.spread_bps, now_ms, event_key=_event_key(model))
                            strength = abs(d.residual_bps)/max(d.threshold_bps,1e-12)
                            if d.ready and strength >= args.min_signal_strength_ratio and abs(d.residual_bps) >= args.min_absolute_residual_bps:
                                stats.signals += 1
                                signal_ms = int(time.time()*1000)
                                sig = Signal(
                                    signal_id=f"xrp-tn-{stats.signals}-{signal_ms}", ts_ms=signal_ms, symbol=SYMBOL,
                                    direction=d.direction, residual_bps=d.residual_bps, threshold_bps=d.threshold_bps,
                                    noise_bps=d.noise_bps, spread_bps=book.spread_bps,
                                    leader_advantage_bps=d.leader_advantage_bps, binance_move_bps=d.binance_move_bps,
                                    mexc_move_bps=d.mexc_move_bps, binance_price=snap.binance_mid, mexc_price=snap.mexc_mid,
                                )
                                console.print(f"SIGNAL #{stats.signals} {SYMBOL} {'LONG' if d.direction>0 else 'SHORT'} residual={d.residual_bps:+.2f}bps strength={strength:.2f}x")
                                ok, why, _, _ = auto._economic_arrival_entry_ok(
                                    signal=sig, current_residual_bps=snap.edge_bps, current_binance_price=snap.binance_mid,
                                    current_spread_bps=book.spread_bps, min_residual_retention=args.min_residual_retention,
                                    min_impulse_retention=args.min_impulse_retention,
                                    min_remaining_edge_bps=args.min_absolute_residual_bps,
                                    min_edge_after_spread_bps=args.min_edge_after_spread_bps,
                                )
                                if not ok:
                                    stats.expired += 1
                                else:
                                    requested, margin, reserve = common._requested_notional(BANK, leverage)
                                    planned = virtual_ioc_fill(book, direction=sig.direction, target_notional_usdt=requested,
                                                               contract_size=live_contract.contract_size, cross_bps=args.ioc_cross_bps)
                                    planned_notional = planned.qty*planned.avg_price
                                    planned_slip = entry_slippage_bps(sig.direction, book, planned.avg_price)
                                    cost = immediate_roundtrip_cost_bps(book, direction=sig.direction, entry_price=planned.avg_price,
                                                                       qty=planned.qty, contract_size=live_contract.contract_size) if planned.qty>0 else math.inf
                                    edge_ok, _ = executable_edge_ok(snap.edge_bps, cost, args.min_executable_net_edge_bps, args.min_edge_to_cost_ratio)
                                    if planned.qty <= 0 or planned_notional < args.min_filled_notional_usdt:
                                        stats.nofill += 1
                                    elif planned_slip > args.max_entry_slippage_bps+1e-9 or not edge_ok:
                                        stats.expired += 1
                                    else:
                                        side = OrderSide.LONG if sig.direction>0 else OrderSide.SHORT
                                        demo_best = await adapter.get_best_price(SYMBOL, side)
                                        limit_price = common._demo_ioc_price(demo_best, side, args.ioc_cross_bps, float(detail.get("priceUnit") or 0))
                                        fill = await adapter.open_ioc(symbol=SYMBOL, side=side, price=limit_price,
                                                                      qty=requested/max(demo_best,1e-12), leverage=leverage,
                                                                      client_order_id=f"xrp-tn-{uuid.uuid4().hex}"[:32])
                                        if fill.filled_qty <= 0:
                                            stats.nofill += 1
                                            console.print(f"TESTNET NO FILL {SYMBOL} requested=${requested:.0f}")
                                        else:
                                            remote = await _resolve_remote_position(adapter, SYMBOL, side, fill, leverage)
                                            entry_ms = int(time.time()*1000)
                                            entry_price = float(remote.entry_price or fill.avg_price or demo_best)
                                            filled_notional = remote.qty*entry_price
                                            actual_slip = max(0.0, _signed_move_bps(sig.direction, demo_best, entry_price))
                                            fresh_book = mexc.books.get(SYMBOL) or book
                                            fresh_snap = model.snapshot(now_ms=entry_ms, threshold_bps=0.0)
                                            stats.entries += 1; stats.fills.append(filled_notional/max(requested,1e-12)); stats.signal_to_fill.append(entry_ms-signal_ms)
                                            pos = Position(sig, remote, fill, signal_ms, entry_ms, fresh_book.mid, fresh_snap.binance_mid,
                                                           fresh_snap.edge_bps, demo_best, entry_price, requested, filled_notional,
                                                           PositiveTrailing(distance_bps=max(args.trailing_distance_bps, fresh_book.spread_bps)),
                                                           last_position_poll_ms=entry_ms)
                                            console.print(
                                                f"TESTNET ENTRY {SYMBOL} requested=${requested:.0f} filled=${filled_notional:.0f} "
                                                f"actual_margin=${filled_notional/leverage:.2f} lev={leverage}x signal_to_fill={entry_ms-signal_ms}ms "
                                                f"DEMO entry_best={demo_best:.10g} fill={entry_price:.10g} slippage={actual_slip:.2f}bps "
                                                f"entry_fee=${fill.fee_usdt:.4f} liq={remote.liquidation_price or 'unknown'}"
                                            )
                                            post_ok, post_why, _, _ = auto._economic_arrival_entry_ok(
                                                signal=sig, current_residual_bps=fresh_snap.edge_bps,
                                                current_binance_price=fresh_snap.binance_mid, current_spread_bps=fresh_book.spread_bps,
                                                min_residual_retention=args.min_residual_retention,
                                                min_impulse_retention=args.min_impulse_retention,
                                                min_remaining_edge_bps=args.min_absolute_residual_bps,
                                                min_edge_after_spread_bps=args.min_edge_after_spread_bps,
                                            )
                                            abort = None
                                            if filled_notional < args.min_filled_notional_usdt: abort="actual_fill_too_small"
                                            elif actual_slip > args.max_entry_slippage_bps+1e-9: abort="actual_entry_slippage"
                                            elif not post_ok: abort=f"arrival_{post_why}"
                                            if abort:
                                                console.print(f"POST-FILL GUARD {SYMBOL} reason={abort}; flattening Demo immediately")
                                                await _close(adapter,pos,stats,output,abort); pos=None

                if pos is not None:
                    book = mexc.books.get(SYMBOL)
                    snap = model.snapshot(now_ms=now_ms, threshold_bps=0.0)
                    if book is not None and _valid_snapshot(snap):
                        age_ms = now_ms-pos.entry_ms
                        mid_move = directional_move_bps(pos.signal.direction,pos.entry_live_mid,book.mid)
                        leader_move = directional_move_bps(pos.signal.direction,pos.entry_binance,snap.binance_mid)
                        residual_dir = 1 if snap.edge_bps>0 else -1 if snap.edge_bps<0 else 0

                        exit_side = OrderSide.SHORT if pos.signal.direction>0 else OrderSide.LONG
                        demo_exit_best = await adapter.get_best_price(SYMBOL, exit_side)
                        executable_pnl_bps = _signed_move_bps(pos.signal.direction,pos.entry_price,demo_exit_best)
                        trail = pos.trailing.update(executable_pnl_bps)
                        pos.mfe_bps=max(pos.mfe_bps,executable_pnl_bps); pos.mae_bps=min(pos.mae_bps,executable_pnl_bps)
                        if not pos.runner_armed and pos.trailing.peak_bps+1e-9 >= args.profit_runner_arm_bps:
                            pos.runner_armed=True
                            console.print(f"PROFIT RUNNER ARMED {SYMBOL} DEMO executable peak={pos.trailing.peak_bps:.2f}bps; convergence disabled")

                        reason=None
                        if age_ms>=args.min_hold_ms:
                            if mid_move<=-args.mid_adverse_cut_bps: reason="mid_adverse_cut"
                            elif leader_move<=-args.leader_retrace_exit_bps: reason="leader_retrace"
                            elif residual_dir==-pos.signal.direction and abs(snap.edge_bps)>=args.reversal_edge_bps: reason="residual_reversal"
                            elif (not pos.runner_armed and abs(snap.edge_bps)<=max(args.convergence_bps,abs(pos.entry_residual_bps)*args.convergence_fraction) and mid_move>=args.min_catchup_bps): reason="mexc_catchup_convergence"
                            elif age_ms>=args.no_progress_ms and mid_move<args.min_progress_bps: reason="no_progress"
                            elif trail is not None and executable_pnl_bps<=trail: reason="positive_trailing_stop"
                            elif age_ms>=args.max_hold_ms: reason="timeout"
                        if reason:
                            console.print(f"EXIT DECISION {SYMBOL} reason={reason} DEMO executable={executable_pnl_bps:+.2f}bps -> submit close immediately")
                            await _close(adapter,pos,stats,output,reason); pos=None

                report=(stats.signals,stats.entries,stats.expired,stats.nofill,stats.wins,stats.losses,stats.flats,round(stats.gross_pnl_usdt,6),round(stats.demo_fees_usdt,6))
                if report!=last_report:
                    console.print("STATE "+_summary(stats)); last_report=report
                wake.clear()
                try: await asyncio.wait_for(wake.wait(),timeout=0.01)
                except TimeoutError: pass
        finally:
            if pos is not None:
                await _close(adapter,pos,stats,output,"shutdown_cleanup")
            await binance.close(); await mexc.close()

    console.print("\n[bold]FINAL FIXED XRP TESTNET REPORT[/bold]")
    console.print(_summary(stats))
    console.print(f"CSV: {output.resolve()}")


def build_parser():
    p=common.build_parser()
    p.description="Fixed XRP-only Testnet plumbing: LIVE signal, Demo execution/PnL, gross-vs-fees accounting"
    return p


def main() -> None:
    args=build_parser().parse_args(); auto.apply_baseline_v1(args)
    try: asyncio.run(run(args))
    except KeyboardInterrupt: raise SystemExit(130)
    except Exception as exc:
        console.print(f"[red]FIXED XRP TESTNET STOPPED:[/red] {type(exc).__name__}: {exc}")
        raise SystemExit(2)


if __name__=="__main__": main()
