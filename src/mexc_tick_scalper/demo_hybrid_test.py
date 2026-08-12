from __future__ import annotations

import argparse
import asyncio
import time
import uuid
from pathlib import Path

from dotenv import load_dotenv
from rich.console import Console

from .demo_smoke import _assert_demo_safety
from .demo_tick_test import _trade_pnl, _wait_for_remote_position
from .execution import OrderFill, OrderSide, PositionSnapshot
from .hybrid_strategy import AsymmetricExitPolicy, MicrostructureSignal, MicrostructureSnapshot
from .market import MexcPublicMarket
from .state import EligibilityState, apply_fee_status
from .web_execution import MexcWebError, MexcWebExecutionAdapter, WebExecutionConfig
from .web_fee import read_web_fee_status

console = Console()
POSITION_WATCHDOG_SECONDS = 0.25


def _load_project_env() -> None:
    project_root = Path(__file__).resolve().parents[2]
    load_dotenv(project_root / ".env", override=False)


def _signed_move_bps(side: OrderSide, entry: float, price: float) -> float:
    if entry <= 0 or price <= 0:
        return 0.0
    raw = (price - entry) / entry * 10_000.0
    return raw if side is OrderSide.LONG else -raw


async def _flatten_position(
    adapter: MexcWebExecutionAdapter,
    position: PositionSnapshot,
    reason: str,
) -> OrderFill:
    close_side = OrderSide.SHORT if position.side is OrderSide.LONG else OrderSide.LONG
    initial_qty = position.qty
    total_closed = 0.0
    total_fee = 0.0
    weighted_price = 0.0
    last_fill: OrderFill | None = None
    remaining = position

    for _ in range(4):
        if remaining.qty <= 1e-12:
            break
        fill = await adapter.close_market_reduce_only(
            symbol=position.symbol,
            qty=remaining.qty,
            side=close_side,
            client_order_id=f"hybrid-exit-{uuid.uuid4().hex}",
        )
        last_fill = fill
        total_fee += fill.fee_usdt

        deadline = time.monotonic() + 1.5
        before_qty = remaining.qty
        observed: PositionSnapshot | None = remaining
        while time.monotonic() < deadline:
            observed = await adapter.get_position(position.symbol)
            if observed is None:
                total_closed += before_qty
                weighted_price += fill.avg_price * before_qty
                remaining = PositionSnapshot(
                    symbol=position.symbol,
                    side=position.side,
                    qty=0.0,
                    entry_price=position.entry_price,
                    leverage=position.leverage,
                    isolated=position.isolated,
                    position_id=position.position_id,
                    liquidation_price=position.liquidation_price,
                    unrealized_pnl=None,
                )
                break
            if observed.side is not position.side:
                raise MexcWebError(f"{reason}: position side changed while closing")
            if observed.qty < before_qty - 1e-12:
                closed_now = before_qty - observed.qty
                total_closed += closed_now
                weighted_price += fill.avg_price * closed_now
                remaining = observed
                break
            await asyncio.sleep(0.05)
        else:
            remaining = observed or remaining

        if remaining.qty <= 1e-12:
            break

        await asyncio.sleep(0.10)
        fresh = await adapter.get_position(position.symbol)
        if fresh is None:
            remaining.qty = 0.0
            break
        remaining = fresh

    residual = await adapter.get_position(position.symbol)
    if residual is not None and residual.qty > 1e-12:
        raise MexcWebError(f"{reason}: residual position remains qty={residual.qty}")

    if total_closed <= 0:
        total_closed = initial_qty
        if last_fill is not None:
            weighted_price = last_fill.avg_price * initial_qty
    avg_price = weighted_price / total_closed if total_closed > 0 else (last_fill.avg_price if last_fill else 0.0)
    assert last_fill is not None
    return OrderFill(
        symbol=last_fill.symbol,
        side=last_fill.side,
        requested_qty=initial_qty,
        filled_qty=initial_qty,
        avg_price=avg_price,
        fee_usdt=total_fee,
        order_id=last_fill.order_id,
        client_order_id=last_fill.client_order_id,
        position_id=last_fill.position_id,
    )


async def run(args: argparse.Namespace) -> None:
    _load_project_env()
    symbol = args.symbol.upper()

    web_cfg = WebExecutionConfig.demo_from_env(write_enabled=True)
    _assert_demo_safety(web_cfg)
    market = MexcPublicMarket(
        "https://futures.testnet.mexc.com",
        "wss://futures.testnet.mexc.com/edge",
    )

    async with MexcWebExecutionAdapter(web_cfg) as adapter:
        if await adapter.get_position(symbol) is not None:
            raise MexcWebError(f"refusing hybrid test: {symbol} already has an open position")

        fee = await read_web_fee_status(adapter, symbol)
        eligibility = apply_fee_status(EligibilityState(symbol), fee, int(time.time() * 1000))
        if not eligibility.can_open_new_position:
            raise MexcWebError(f"zero fee not confirmed: maker={fee.maker} taker={fee.taker} source={fee.source}")

        detail = await adapter.get_contract_detail(symbol)
        contract_size = float(detail.get("contractSize") or 0)
        min_vol = float(detail.get("minVol") or 0)
        max_leverage = int(detail.get("maxLeverage") or 1)
        if contract_size <= 0 or min_vol <= 0:
            raise MexcWebError("invalid contract sizing metadata")

        min_base_qty = contract_size * min_vol
        leverage = min(max(1, int(args.leverage)), max_leverage)
        target_margin = max(0.01, float(args.target_margin_usdt))
        target_notional = target_margin * leverage
        signal = MicrostructureSignal(window_seconds=args.signal_window_seconds, min_trade_rate=args.min_trade_rate)
        neutral_snap = MicrostructureSnapshot(0, 0.0, 0.0, 0.5, 0.0, 0.0, 0)
        last_snap = neutral_snap

        position: PositionSnapshot | None = None
        exit_policy: AsymmetricExitPolicy | None = None
        entry_side: OrderSide | None = None
        entry_price = 0.0
        entry_fee = 0.0
        entry_time = 0.0
        mfe_bps = 0.0
        mae_bps = 0.0
        session_pnl = 0.0
        gross_profit = 0.0
        gross_loss = 0.0
        wins = losses = cycles = signals_seen = raw_ticks = 0
        deadline = time.monotonic() + int(args.session_seconds)
        next_fee_check = 0.0
        next_heartbeat = 0.0
        latched_snap: MicrostructureSnapshot | None = None
        latched_until = 0.0

        trade_queue: asyncio.Queue = asyncio.Queue(maxsize=20_000)

        async def pump_trades() -> None:
            async for market_tick in market.trades(symbol):
                if trade_queue.full():
                    try:
                        trade_queue.get_nowait()
                    except asyncio.QueueEmpty:
                        pass
                trade_queue.put_nowait(market_tick)

        producer = asyncio.create_task(pump_trades())

        console.print(
            f"HYBRID DEMO {symbol}: target_margin={target_margin:g} USDT target_notional={target_notional:g} USDT "
            f"leverage={leverage}x confidence>={args.min_confidence:.2f} early_adverse={args.early_adverse_changes} "
            f"liq_buffer={args.liq_buffer_fraction:.0%} signal_ttl={args.signal_ttl_ms:.0f}ms "
            f"position_watchdog={POSITION_WATCHDOG_SECONDS:.2f}s"
        )
        console.print("SCANNING: waiting for LIVE market signal ticks; Demo executable-price watchdog is independent.")

        try:
            while time.monotonic() < deadline and cycles < int(args.max_cycles):
                wait_seconds = POSITION_WATCHDOG_SECONDS if position is not None else 1.0
                tick = None
                try:
                    tick = await asyncio.wait_for(trade_queue.get(), timeout=wait_seconds)
                except TimeoutError:
                    pass

                now = time.monotonic()
                now_ms = int(time.time() * 1000)
                signal_fresh = tick is not None
                if signal_fresh:
                    raw_ticks += 1
                    last_snap = signal.update(tick)
                snap = last_snap

                if now >= next_fee_check:
                    fee = await read_web_fee_status(adapter, symbol)
                    eligibility = apply_fee_status(eligibility, fee, now_ms)
                    next_fee_check = now + float(args.fee_check_seconds)

                valid_signal = (
                    signal_fresh
                    and eligibility.can_open_new_position
                    and snap.trade_rate >= float(args.min_trade_rate)
                    and snap.price_changes >= int(args.min_price_changes)
                    and snap.direction != 0
                    and snap.confidence >= float(args.min_confidence)
                )
                if position is None and valid_signal:
                    latched_snap = snap
                    latched_until = now + float(args.signal_ttl_ms) / 1000.0
                    console.print(
                        f"LATCHED SIGNAL direction={snap.direction:+d} confidence={snap.confidence:.3f} "
                        f"rate={snap.trade_rate:.2f}/s price_changes={snap.price_changes} ttl={args.signal_ttl_ms:.0f}ms"
                    )

                if now >= next_heartbeat:
                    blockers: list[str] = []
                    if not eligibility.can_open_new_position:
                        blockers.append("fee")
                    if snap.trade_rate < float(args.min_trade_rate):
                        blockers.append("rate")
                    if snap.price_changes < int(args.min_price_changes):
                        blockers.append("price_changes")
                    if snap.direction == 0:
                        blockers.append("direction")
                    if snap.confidence < float(args.min_confidence):
                        blockers.append("confidence")
                    spread_txt = "?"
                    mark_txt = ""
                    try:
                        ask_hb = await adapter.get_best_price(symbol, OrderSide.LONG)
                        bid_hb = await adapter.get_best_price(symbol, OrderSide.SHORT)
                        mid_hb = (ask_hb + bid_hb) / 2.0
                        spread_bps = ((ask_hb - bid_hb) / mid_hb) * 10_000 if mid_hb > 0 else 99999.0
                        spread_txt = f"{spread_bps:.3f}"
                        if spread_bps > float(args.max_spread_bps):
                            blockers.append("spread")
                        if position is not None and entry_side is not None:
                            executable_hb = bid_hb if entry_side is OrderSide.LONG else ask_hb
                            mark_bps = _signed_move_bps(entry_side, entry_price, executable_hb)
                            mark_txt = f" mark={mark_bps:+.3f}bps"
                    except MexcWebError:
                        blockers.append("book")
                    if position is not None:
                        state = "IN_POSITION"
                    elif latched_snap is not None and now <= latched_until:
                        state = "LATCHED"
                    else:
                        state = "READY" if not blockers else "BLOCKED:" + ",".join(blockers)
                    console.print(
                        f"HEARTBEAT ticks={raw_ticks} rate={snap.trade_rate:.2f}/s confidence={snap.confidence:.3f} "
                        f"direction={snap.direction:+d} price_changes={snap.price_changes} spread={spread_txt}bps "
                        f"fee={fee.maker}/{fee.taker} state={state}{mark_txt}"
                    )
                    next_heartbeat = now + float(args.heartbeat_seconds)

                if position is not None:
                    assert exit_policy is not None
                    assert entry_side is not None
                    close_side = OrderSide.SHORT if entry_side is OrderSide.LONG else OrderSide.LONG
                    executable_price = await adapter.get_best_price(symbol, close_side)
                    move_bps = _signed_move_bps(entry_side, entry_price, executable_price)
                    mfe_bps = max(mfe_bps, move_bps)
                    mae_bps = min(mae_bps, move_bps)

                    reason = exit_policy.on_tick(
                        price=executable_price,
                        liquidation_price=position.liquidation_price,
                        signal=snap,
                        age_seconds=now - entry_time,
                        signal_fresh=signal_fresh,
                    )
                    if reason is not None:
                        fill = await _flatten_position(adapter, position, reason)
                        fees = entry_fee + fill.fee_usdt
                        pnl_usdt, price_pct, roe_pct = _trade_pnl(
                            entry_side, entry_price, fill.avg_price, fill.filled_qty, leverage, fees
                        )
                        session_pnl += pnl_usdt
                        duration = now - entry_time
                        if pnl_usdt > 0:
                            wins += 1
                            gross_profit += pnl_usdt
                        elif pnl_usdt < 0:
                            losses += 1
                            gross_loss += abs(pnl_usdt)
                        peak_roe = mfe_bps / 100.0 * leverage
                        worst_roe = mae_bps / 100.0 * leverage
                        giveback_roe = peak_roe - roe_pct
                        console.print(
                            f"EXIT reason={reason} mark={executable_price:g} qty={fill.filled_qty:g} "
                            f"avg={fill.avg_price:g} fee={fill.fee_usdt:g}"
                        )
                        console.print(
                            f"RESULT pnl={pnl_usdt:+.6f} USDT price={price_pct:+.4f}% ROE={roe_pct:+.2f}% "
                            f"MFE={mfe_bps:+.3f}bps MAE={mae_bps:+.3f}bps peak_ROE={peak_roe:+.2f}% "
                            f"worst_ROE={worst_roe:+.2f}% giveback_ROE={giveback_roe:+.2f}% "
                            f"duration={duration:.2f}s session_pnl={session_pnl:+.6f}"
                        )
                        cycles += 1
                        position = None
                        exit_policy = None
                        entry_side = None
                        entry_price = entry_fee = entry_time = 0.0
                        mfe_bps = mae_bps = 0.0
                        latched_snap = None
                        latched_until = 0.0
                        if fill.fee_usdt != 0:
                            raise MexcWebError(f"non-zero execution fee observed on exit: {fill.fee_usdt}")
                    continue

                active_snap = latched_snap if (latched_snap is not None and now <= latched_until) else None
                if active_snap is None:
                    latched_snap = None
                    continue

                residual = await adapter.get_position(symbol)
                if residual is not None:
                    raise MexcWebError(f"residual position detected before entry qty={residual.qty}; refusing to stack")

                side = OrderSide.LONG if active_snap.direction == 1 else OrderSide.SHORT
                ask = await adapter.get_best_price(symbol, OrderSide.LONG)
                bid = await adapter.get_best_price(symbol, OrderSide.SHORT)
                mid = (ask + bid) / 2.0
                spread_bps = ((ask - bid) / mid) * 10_000 if mid > 0 else 99999.0
                if spread_bps > float(args.max_spread_bps):
                    latched_snap = None
                    latched_until = 0.0
                    continue
                best_price = ask if side is OrderSide.LONG else bid
                requested_qty = max(min_base_qty, target_notional / best_price)

                signals_seen += 1
                fill = await adapter.open_ioc(
                    symbol=symbol,
                    side=side,
                    price=best_price,
                    qty=requested_qty,
                    leverage=leverage,
                    client_order_id=f"hybrid-entry-{uuid.uuid4().hex}",
                )
                latched_snap = None
                latched_until = 0.0
                if fill.filled_qty <= 0:
                    continue
                if fill.filled_qty > requested_qty * 1.000001:
                    raise MexcWebError(
                        f"impossible IOC fill: requested={requested_qty} filled={fill.filled_qty}; refusing inconsistent state"
                    )
                if fill.fee_usdt != 0:
                    raise MexcWebError(f"non-zero execution fee observed on entry: {fill.fee_usdt}")

                remote = await _wait_for_remote_position(adapter, symbol)
                if remote is None:
                    raise MexcWebError("IOC fill reported but position did not appear in open_positions within 1s")
                if remote.qty > requested_qty * 1.000001:
                    raise MexcWebError(
                        f"remote position exceeds IOC request: requested={requested_qty} remote={remote.qty}; refusing to continue"
                    )

                position = remote
                entry_side = side
                entry_price = remote.entry_price or fill.avg_price
                entry_fee = fill.fee_usdt
                entry_time = now
                mfe_bps = mae_bps = 0.0
                effective_pullback_bps = max(float(args.winner_pullback_bps), spread_bps)
                effective_flip_pullback_bps = max(0.5, spread_bps * 0.5)
                exit_policy = AsymmetricExitPolicy(
                    side=1 if side is OrderSide.LONG else -1,
                    entry_price=entry_price,
                    early_adverse_changes=int(args.early_adverse_changes),
                    liq_buffer_fraction=float(args.liq_buffer_fraction),
                    winner_arm_bps=float(args.winner_arm_bps),
                    winner_pullback_bps=effective_pullback_bps,
                    winner_flip_pullback_bps=effective_flip_pullback_bps,
                    flip_confidence=float(args.exit_flip_confidence),
                    fade_confidence=float(args.exit_fade_confidence),
                    min_hold_seconds=float(args.min_hold_seconds),
                )
                fill_ratio = remote.qty / requested_qty if requested_qty > 0 else 0.0
                console.print(
                    f"ENTRY {'LONG' if side is OrderSide.LONG else 'SHORT'} requested={requested_qty:g} "
                    f"filled={remote.qty:g} fill_ratio={fill_ratio:.1%} entry={entry_price:g} "
                    f"liq={remote.liquidation_price if remote.liquidation_price is not None else '?'} fee={fill.fee_usdt:g}"
                )
                console.print(
                    f"SIGNAL confidence={active_snap.confidence:.3f} momentum={active_snap.momentum_bps:+.3f}bps "
                    f"CVD={active_snap.cvd_norm:+.3f} buy_ratio={active_snap.buy_ratio:.3f} rate={active_snap.trade_rate:.2f}/s "
                    f"spread={spread_bps:.3f}bps trailing={effective_pullback_bps:.3f}bps "
                    f"flip_pullback={effective_flip_pullback_bps:.3f}bps"
                )
        finally:
            producer.cancel()
            try:
                await producer
            except asyncio.CancelledError:
                pass

        if position is not None:
            fill = await _flatten_position(adapter, position, "session_timeout")
            fees = entry_fee + fill.fee_usdt
            pnl_usdt, price_pct, roe_pct = _trade_pnl(
                entry_side or position.side, entry_price, fill.avg_price, fill.filled_qty, leverage, fees
            )
            session_pnl += pnl_usdt
            peak_roe = mfe_bps / 100.0 * leverage
            worst_roe = mae_bps / 100.0 * leverage
            giveback_roe = peak_roe - roe_pct
            console.print(f"TIMEOUT FLATTEN qty={fill.filled_qty:g} avg={fill.avg_price:g} fee={fill.fee_usdt:g}")
            console.print(
                f"RESULT pnl={pnl_usdt:+.6f} USDT price={price_pct:+.4f}% ROE={roe_pct:+.2f}% "
                f"MFE={mfe_bps:+.3f}bps MAE={mae_bps:+.3f}bps peak_ROE={peak_roe:+.2f}% "
                f"worst_ROE={worst_roe:+.2f}% giveback_ROE={giveback_roe:+.2f}% "
                f"session_pnl={session_pnl:+.6f}"
            )

        pf = gross_profit / gross_loss if gross_loss > 0 else (float("inf") if gross_profit > 0 else 0.0)
        win_rate = wins / max(1, wins + losses) * 100.0
        console.print(
            f"HYBRID DEMO COMPLETE cycles={cycles} signals={signals_seen} ticks={raw_ticks} wins={wins} losses={losses} "
            f"win_rate={win_rate:.1f}% PF={pf:.2f} session_pnl={session_pnl:+.6f} USDT"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="MEXC Demo reconstructed IOC + microstructure strategy")
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--session-seconds", type=int, default=300)
    parser.add_argument("--max-cycles", type=int, default=10)
    parser.add_argument("--leverage", type=int, default=50)
    parser.add_argument("--target-margin-usdt", type=float, default=2.0)
    parser.add_argument("--signal-window-seconds", type=float, default=5.0)
    parser.add_argument("--min-trade-rate", type=float, default=0.5)
    parser.add_argument("--min-confidence", type=float, default=0.35)
    parser.add_argument("--min-price-changes", type=int, default=3)
    parser.add_argument("--max-spread-bps", type=float, default=10.0)
    parser.add_argument("--early-adverse-changes", type=int, default=2)
    parser.add_argument("--winner-arm-bps", type=float, default=0.5)
    parser.add_argument("--winner-pullback-bps", type=float, default=1.5)
    parser.add_argument("--exit-flip-confidence", type=float, default=0.30)
    parser.add_argument("--exit-fade-confidence", type=float, default=0.12)
    parser.add_argument("--min-hold-seconds", type=float, default=0.35)
    parser.add_argument("--liq-buffer-fraction", type=float, default=0.25)
    parser.add_argument("--fee-check-seconds", type=float, default=15.0)
    parser.add_argument("--heartbeat-seconds", type=float, default=3.0)
    parser.add_argument("--signal-ttl-ms", type=float, default=500.0)
    args = parser.parse_args()
    try:
        asyncio.run(run(args))
    except MexcWebError as exc:
        console.print(f"[red]HYBRID DEMO FAILED:[/red] {exc}")
        raise SystemExit(2) from exc


if __name__ == "__main__":
    main()
