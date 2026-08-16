from __future__ import annotations

import argparse
import asyncio
import math
import time
import uuid
from pathlib import Path

from rich.console import Console

from .execution import OrderSide
from .lead_lag_strategy import LeadLagGate, convergence_threshold, spread_aware_adverse_cut
from .live_lead_lag_shadow import PositiveTrailing
from .live_production_runner import (
    FeeCache,
    LivePosition,
    _append_trade,
    _assert_live_write_config,
    _close_position_fully,
    _emergency_close,
    _fee_loop,
    _load_env,
    _marketable_ioc_price,
    _resolve_remote_position,
    _signed_move_bps,
)
from .live_zero_fee_universe import discover_live_zero_fee_crosslisted
from .microspread import MicroSpreadModel
from .microspread_feed import EventBinanceBookTickerFeed, EventMexcDepthFeed
from .web_execution import MexcWebError, MexcWebExecutionAdapter, WebExecutionConfig

console = Console()


def _event_key(model: MicroSpreadModel) -> tuple[int, int] | None:
    if not model.binance or not model.mexc:
        return None
    return int(model.binance[-1][0]), int(model.mexc[-1][0])


def _valid_snapshot(snap) -> bool:
    return snap.reason not in {
        "warming_up", "warming_baseline", "warming_horizon",
        "stale_binance", "stale_mexc",
    }


async def run(args: argparse.Namespace) -> None:
    _load_env()
    if args.confirm_live != "LIVE":
        raise MexcWebError("Real trading requires --confirm-live LIVE")

    contracts = await discover_live_zero_fee_crosslisted()
    included = {x.strip().upper() for x in args.include_symbols.split(",") if x.strip()}
    excluded = {x.strip().upper() for x in args.exclude_symbols.split(",") if x.strip()}
    if included:
        contracts = [x for x in contracts if x.mexc_symbol in included]
    if excluded:
        contracts = [x for x in contracts if x.mexc_symbol not in excluded]
    if not contracts:
        raise MexcWebError("No currently exact-0/0 LIVE Binance-crosslisted symbols remain after filters")

    symbols = [x.mexc_symbol for x in contracts]
    models = {
        x.mexc_symbol: MicroSpreadModel(
            horizon_ms=args.micro_horizon_ms,
            baseline_seconds=args.baseline_seconds,
            baseline_exclusion_ms=args.baseline_exclusion_ms,
            min_edge_bps=0.0,
            min_binance_move_bps=0.0,
            max_binance_age_ms=args.max_binance_age_ms,
            max_mexc_age_ms=args.max_mexc_age_ms,
            rearm_fraction=0.35,
        ) for x in contracts
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
    mexc = EventMexcDepthFeed(symbols, models, wake, depth_limit=5)
    await binance.start()
    await mexc.start()

    fee_cache = FeeCache()
    fee_stop = asyncio.Event()
    fee_task = asyncio.create_task(_fee_loop(fee_cache, fee_stop))
    cfg = WebExecutionConfig.from_env(write_enabled=True)
    _assert_live_write_config(cfg)

    position: LivePosition | None = None
    cycles = 0
    realized_pnl_usdt = 0.0
    realized_pnl_bps = 0.0
    peak_realized_usdt = 0.0
    max_drawdown_usdt = 0.0
    warmup_until = time.monotonic() + args.warmup_seconds
    deadline = time.monotonic() + args.session_seconds
    next_heartbeat = 0.0
    last_entry_ms = {symbol: -10**18 for symbol in symbols}
    contract_by_symbol: dict[str, tuple[float, int, float, float]] = {}
    log_path = Path(args.trade_csv or f"live_trades_v2_{int(time.time())}.csv")

    try:
        async with MexcWebExecutionAdapter(cfg) as adapter:
            existing = await adapter.get_positions()
            if existing and not args.allow_existing_positions:
                labels = ", ".join(f"{x.symbol}:{x.side.value}:{x.qty:g}" for x in existing[:8])
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
            symbols = [s for s in symbols if s in contract_by_symbol]
            if not symbols:
                raise MexcWebError("No LIVE symbol has valid sizing metadata")

            console.print(
                f"[bold red]LIVE V2 BINANCE-LEAD/MEXC-LAG[/bold red]: {len(symbols)} exact-0/0 symbols; "
                f"adaptive noise x{args.residual_noise_multiplier:g}, confirm={args.confirm_updates} updates/{args.confirm_ms}ms"
            )
            console.print(f"Trade log: {log_path.resolve()}")

            try:
                while time.monotonic() < deadline and cycles < args.max_cycles:
                    now = time.monotonic()
                    now_ms = int(time.time() * 1000)

                    if position is None and now >= warmup_until:
                        candidates = []
                        for symbol in symbols:
                            if not fee_cache.fresh_zero(symbol, now_ms):
                                continue
                            book = mexc.books.get(symbol)
                            if book is None or now_ms - book.recv_ms > args.max_book_age_ms:
                                continue
                            if now_ms - last_entry_ms[symbol] < args.entry_cooldown_ms:
                                continue
                            model = models[symbol]
                            snap = model.snapshot(now_ms=now_ms, threshold_bps=0.0)
                            if not _valid_snapshot(snap):
                                continue
                            decision = gate.observe(
                                symbol, snap, book.spread_bps, now_ms, event_key=_event_key(model)
                            )
                            if decision.ready:
                                candidates.append((
                                    abs(decision.residual_bps) - decision.threshold_bps,
                                    -book.spread_bps,
                                    symbol,
                                    decision,
                                    book,
                                ))

                        if candidates:
                            _, _, symbol, decision, book = max(candidates, key=lambda x: (x[0], x[1]))
                            if not fee_cache.fresh_zero(symbol, int(time.time() * 1000)):
                                continue
                            min_qty, max_lev, max_qty, price_unit = contract_by_symbol[symbol]
                            side = OrderSide.LONG if decision.direction > 0 else OrderSide.SHORT
                            leverage = min(max(1, int(args.leverage)), max_lev)
                            limit_price = _marketable_ioc_price(side, book, args.ioc_cross_bps, price_unit)
                            requested_qty = min(max_qty, max(min_qty, args.target_notional_usdt / limit_price))
                            signal_ms = time.time_ns() / 1_000_000.0
                            fill = await adapter.open_ioc(
                                symbol=symbol,
                                side=side,
                                price=limit_price,
                                qty=requested_qty,
                                leverage=leverage,
                                client_order_id=f"live-v2-entry-{uuid.uuid4().hex}"[:32],
                            )
                            result_ms = time.time_ns() / 1_000_000.0
                            if fill.filled_qty > 0:
                                remote = await _resolve_remote_position(adapter, symbol, side, fill, leverage)
                                position = LivePosition(
                                    remote=remote,
                                    direction=decision.direction,
                                    entry_edge_bps=decision.residual_bps,
                                    entry_spread_bps=book.spread_bps,
                                    opened_monotonic=time.monotonic(),
                                    entry_ms=int(time.time() * 1000),
                                    entry_fill=fill,
                                    trailing=PositiveTrailing(distance_bps=max(args.trailing_distance_bps, book.spread_bps)),
                                )
                                last_entry_ms[symbol] = int(time.time() * 1000)
                                console.print(
                                    f"[green]LIVE ENTRY[/green] {symbol} {'LONG' if decision.direction > 0 else 'SHORT'} "
                                    f"fill={remote.entry_price:g} qty={remote.qty:g} lev={leverage}x "
                                    f"residual={decision.residual_bps:+.3f} threshold={decision.threshold_bps:.3f} "
                                    f"noise={decision.noise_bps:.3f} lead={decision.leader_advantage_bps:.3f}bps "
                                    f"signal_to_ioc_result={result_ms-signal_ms:.1f}ms fee={fill.fee_usdt:g}"
                                )
                                if fill.fee_usdt != 0.0:
                                    await _emergency_close(adapter, position, "entry_fee_nonzero")
                                    position = None
                                    raise MexcWebError(f"LIVE zero-fee invariant violated on entry: fee={fill.fee_usdt:g}")

                    elif position is not None:
                        symbol = position.remote.symbol
                        book = mexc.books.get(symbol)
                        snap = models[symbol].snapshot(now_ms=now_ms, threshold_bps=0.0)
                        age_s = now - position.opened_monotonic
                        reason: str | None = None
                        move_bps = 0.0

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
                            conv = convergence_threshold(
                                position.entry_edge_bps, args.convergence_bps, args.convergence_fraction
                            )
                            adverse = spread_aware_adverse_cut(
                                position.entry_spread_bps, args.adverse_cut_bps, args.adverse_spread_multiple
                            )
                            residual_direction = 1 if snap.edge_bps > 0 else -1 if snap.edge_bps < 0 else 0

                            # Primary strategy exit: MEXC has caught Binance and residual returned to normal basis.
                            if abs(snap.edge_bps) <= conv and age_s >= args.min_hold_seconds:
                                reason = "convergence"
                            elif (
                                residual_direction == -position.direction
                                and abs(snap.edge_bps) >= args.reversal_edge_bps
                                and age_s >= args.min_hold_seconds
                            ):
                                reason = "residual_reversal"
                            elif trail is not None and move_bps <= trail and age_s >= args.min_hold_seconds:
                                reason = "positive_trailing_stop"
                            elif move_bps <= -adverse and age_s >= args.min_hold_seconds:
                                reason = "spread_aware_adverse_cut"
                            elif age_s >= args.max_hold_seconds:
                                reason = "timeout"

                        if reason is not None:
                            exit_fill = await _close_position_fully(adapter, position.remote)
                            exit_ms = int(time.time() * 1000)
                            exit_price = float(exit_fill.avg_price or position.remote.entry_price)
                            pnl_bps = _signed_move_bps(position.direction, position.remote.entry_price, exit_price)
                            pnl_usdt = (
                                position.direction * (exit_price - position.remote.entry_price) * position.remote.qty
                                - position.entry_fill.fee_usdt - exit_fill.fee_usdt
                            )
                            realized_pnl_bps += pnl_bps
                            realized_pnl_usdt += pnl_usdt
                            peak_realized_usdt = max(peak_realized_usdt, realized_pnl_usdt)
                            max_drawdown_usdt = max(max_drawdown_usdt, peak_realized_usdt - realized_pnl_usdt)
                            _append_trade(log_path, {
                                "entry_ms": position.entry_ms, "exit_ms": exit_ms, "symbol": symbol,
                                "direction": "LONG" if position.direction > 0 else "SHORT", "qty": position.remote.qty,
                                "leverage": position.remote.leverage, "entry_price": position.remote.entry_price,
                                "exit_price": exit_price, "entry_edge_bps": position.entry_edge_bps,
                                "entry_spread_bps": position.entry_spread_bps, "pnl_bps": pnl_bps,
                                "pnl_usdt": pnl_usdt, "mfe_bps": position.mfe_bps, "mae_bps": position.mae_bps,
                                "hold_ms": int(age_s * 1000), "exit_reason": reason,
                                "entry_fee_usdt": position.entry_fill.fee_usdt, "exit_fee_usdt": exit_fill.fee_usdt,
                                "entry_order_id": position.entry_fill.order_id, "exit_order_id": exit_fill.order_id,
                            })
                            console.print(
                                f"[cyan]LIVE EXIT[/cyan] {symbol} reason={reason} pnl={pnl_bps:+.3f}bps/{pnl_usdt:+.6f}USDT "
                                f"residual={snap.edge_bps:+.3f} MFE={position.mfe_bps:+.3f} MAE={position.mae_bps:+.3f} "
                                f"hold={age_s:.3f}s fees={position.entry_fill.fee_usdt + exit_fill.fee_usdt:g}"
                            )
                            position = None
                            cycles += 1
                            if realized_pnl_usdt <= -abs(args.max_session_loss_usdt):
                                raise MexcWebError("session loss kill-switch hit")

                    if now >= next_heartbeat:
                        console.print(
                            f"LIVE V2 HEARTBEAT state={'POSITION' if position else 'WATCHING'} cycles={cycles}/{args.max_cycles} "
                            f"books={len(mexc.books)}/{len(symbols)} Bquotes={binance.quotes} Mdepth={mexc.updates} "
                            f"pnl={realized_pnl_usdt:+.6f}USDT dd={max_drawdown_usdt:.6f}USDT"
                        )
                        next_heartbeat = now + args.heartbeat_seconds

                    wake.clear()
                    try:
                        await asyncio.wait_for(wake.wait(), timeout=0.05 if position else 0.5)
                    except TimeoutError:
                        pass
            except asyncio.CancelledError:
                if position is not None:
                    await _emergency_close(adapter, position, "task_cancelled")
                    position = None
                raise
            finally:
                if position is not None:
                    await _emergency_close(adapter, position, "runner_shutdown")
                    position = None
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
    p = argparse.ArgumentParser(description="Noise-filtered real-money Binance-lead/MEXC-lag convergence runner")
    p.add_argument("--confirm-live", default="")
    p.add_argument("--include-symbols", default="")
    p.add_argument("--exclude-symbols", default="")
    p.add_argument("--allow-existing-positions", action="store_true")
    p.add_argument("--session-seconds", type=float, default=3600.0)
    p.add_argument("--max-cycles", type=int, default=20)
    p.add_argument("--warmup-seconds", type=float, default=10.0)
    p.add_argument("--target-notional-usdt", type=float, default=10.0)
    p.add_argument("--leverage", type=int, default=1000)
    p.add_argument("--micro-horizon-ms", type=int, default=100)
    p.add_argument("--baseline-seconds", type=float, default=8.0)
    p.add_argument("--baseline-exclusion-ms", type=int, default=1000)
    p.add_argument("--noise-window-ms", type=int, default=8000)
    p.add_argument("--residual-noise-multiplier", type=float, default=3.0)
    p.add_argument("--binance-noise-multiplier", type=float, default=1.0)
    p.add_argument("--min-edge-bps", type=float, default=0.50)
    p.add_argument("--min-net-edge-bps", type=float, default=0.20)
    p.add_argument("--edge-to-spread-ratio", type=float, default=1.05)
    p.add_argument("--min-binance-move-bps", type=float, default=0.50)
    p.add_argument("--min-leader-advantage-bps", type=float, default=0.25)
    p.add_argument("--min-lead-ratio", type=float, default=1.25)
    p.add_argument("--confirm-updates", type=int, default=2)
    p.add_argument("--confirm-ms", type=int, default=15)
    p.add_argument("--rearm-fraction", type=float, default=0.35)
    p.add_argument("--max-binance-age-ms", type=float, default=300.0)
    p.add_argument("--max-mexc-age-ms", type=float, default=2000.0)
    p.add_argument("--max-book-age-ms", type=float, default=750.0)
    p.add_argument("--position-data-stale-ms", type=float, default=3000.0)
    p.add_argument("--entry-cooldown-ms", type=int, default=500)
    p.add_argument("--ioc-cross-bps", type=float, default=1.0)
    p.add_argument("--min-hold-seconds", type=float, default=0.05)
    p.add_argument("--max-hold-seconds", type=float, default=15.0)
    p.add_argument("--convergence-bps", type=float, default=0.10)
    p.add_argument("--convergence-fraction", type=float, default=0.20)
    p.add_argument("--reversal-edge-bps", type=float, default=0.35)
    p.add_argument("--adverse-cut-bps", type=float, default=1.5)
    p.add_argument("--adverse-spread-multiple", type=float, default=1.25)
    p.add_argument("--trailing-distance-bps", type=float, default=1.5)
    p.add_argument("--max-session-loss-usdt", type=float, default=2.0)
    p.add_argument("--heartbeat-seconds", type=float, default=5.0)
    p.add_argument("--trade-csv", default="")
    return p


def main() -> None:
    args = build_parser().parse_args()
    if args.target_notional_usdt <= 0:
        raise SystemExit("--target-notional-usdt must be positive")
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        console.print("\n[yellow]LIVE V2 stop requested.[/yellow]")
        raise SystemExit(130)
    except Exception as exc:
        console.print(f"[red]LIVE V2 STOPPED:[/red] {type(exc).__name__}: {exc}")
        raise SystemExit(2)


if __name__ == "__main__":
    main()
