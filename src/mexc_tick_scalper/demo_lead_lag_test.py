from __future__ import annotations

import argparse
import asyncio
import time
import uuid

from . import demo_hybrid_test as hybrid
from .demo_live_signal_test import _GuardedDemoAdapter, _SpreadAwareExitPolicy
from .demo_smoke import _assert_demo_safety
from .demo_tick_test import _trade_pnl
from .execution import OrderSide, PositionSnapshot
from .hybrid_strategy import MicrostructureSignal, MicrostructureSnapshot
from .lead_lag import BinanceBookTickerFeed, LeadLagModel
from .market import MexcPublicMarket
from .state import EligibilityState, apply_fee_status
from .web_execution import MexcWebError, WebExecutionConfig
from .web_fee import read_web_fee_status

LIVE_REST = "https://contract.mexc.com"
LIVE_WS = "wss://contract.mexc.com/edge"
LOOP_SECONDS = 0.05
POSITION_PRICE_SECONDS = 0.10
FLOW_VETO_CONFIDENCE = 0.60


async def _wait_remote_position(
    adapter: _GuardedDemoAdapter,
    symbol: str,
    side: OrderSide,
    *,
    timeout_seconds: float = 5.0,
) -> PositionSnapshot | None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        remote = await adapter.get_position(symbol)
        if remote is not None:
            if remote.side is not side:
                raise MexcWebError(
                    f"lead-lag position side mismatch: requested={side.value} remote={remote.side.value}"
                )
            return remote
        await asyncio.sleep(0.08)
    return None


def _zero_fee_confirmed(fee) -> bool:
    return float(fee.maker) == 0.0 and float(fee.taker) == 0.0


async def run(args: argparse.Namespace) -> None:
    hybrid._load_project_env()
    symbol = args.symbol.upper()

    web_cfg = WebExecutionConfig.demo_from_env(write_enabled=True)
    _assert_demo_safety(web_cfg)

    market = MexcPublicMarket(LIVE_REST, LIVE_WS)
    model = LeadLagModel(
        horizon_ms=args.lead_horizon_ms,
        baseline_seconds=args.baseline_seconds,
        min_edge_bps=args.min_edge_bps,
        min_binance_move_bps=args.min_binance_move_bps,
        max_age_ms=args.max_quote_age_ms,
    )
    binance_feed = BinanceBookTickerFeed(symbol, model)
    flow = MicrostructureSignal(window_seconds=5.0, min_trade_rate=0.0)
    latest_flow = MicrostructureSnapshot(0, 0.0, 0.0, 0.5, 0.0, 0.0, 0)
    flow_version = 0
    consumed_flow_version = 0

    trade_queue: asyncio.Queue = asyncio.Queue(maxsize=20_000)

    async def pump_mexc() -> None:
        async for tick in market.trades(symbol):
            if trade_queue.full():
                try:
                    trade_queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            trade_queue.put_nowait(tick)

    producer = asyncio.create_task(pump_mexc())
    await binance_feed.start()

    position: PositionSnapshot | None = None
    exit_policy: _SpreadAwareExitPolicy | None = None
    entry_side: OrderSide | None = None
    entry_price = entry_fee = entry_time = entry_edge_bps = 0.0
    mfe_bps = mae_bps = 0.0
    cycles = wins = losses = signals_seen = raw_ticks = 0
    session_pnl = peak_session_pnl = max_drawdown_usdt = 0.0
    gross_profit = gross_loss = 0.0
    deadline = time.monotonic() + int(args.session_seconds)
    warmup_until = time.monotonic() + float(args.warmup_seconds)
    next_fee_check = 0.0
    next_position_price = 0.0
    next_heartbeat = 0.0
    last_executable_price = 0.0
    fee = None
    eligibility = EligibilityState(symbol)

    hybrid.console.print(
        f"LEAD-LAG DEMO {symbol}: Binance=leader MEXC=lagger TESTNET execution; "
        f"edge>={args.min_edge_bps:.2f}bps Bmove>={args.min_binance_move_bps:.2f}bps "
        f"horizon={args.lead_horizon_ms}ms warmup={args.warmup_seconds:g}s loop={LOOP_SECONDS:.2f}s"
    )
    hybrid.console.print(
        "ZERO-FEE HARD GATE: no new position unless current Demo maker/taker fees are exactly 0/0."
    )

    try:
        async with _GuardedDemoAdapter(web_cfg) as adapter:
            existing = await adapter.get_position(symbol)
            if existing is not None:
                raise MexcWebError(f"refusing lead-lag test: {symbol} already has open qty={existing.qty}")

            detail = await adapter.get_contract_detail(symbol)
            contract_size = float(detail.get("contractSize") or 0)
            min_vol = float(detail.get("minVol") or 0)
            max_leverage = int(detail.get("maxLeverage") or 1)
            if contract_size <= 0 or min_vol <= 0:
                raise MexcWebError("invalid contract sizing metadata")
            min_base_qty = contract_size * min_vol
            leverage = min(max(1, int(args.leverage)), max_leverage)
            target_notional = max(0.01, float(args.target_margin_usdt)) * leverage

            while time.monotonic() < deadline and cycles < int(args.max_cycles):
                loop_started = time.monotonic()
                signal_fresh = False
                while True:
                    try:
                        tick = trade_queue.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                    raw_ticks += 1
                    now_local_ms = int(time.time() * 1000)
                    model.update_mexc_price(price=tick.price, ts_ms=now_local_ms)
                    latest_flow = flow.update(tick)
                    flow_version += 1
                    signal_fresh = True

                now = time.monotonic()
                now_ms = int(time.time() * 1000)
                lead = model.snapshot(now_ms=now_ms)

                if now >= next_fee_check:
                    fee = await read_web_fee_status(adapter, symbol)
                    eligibility = apply_fee_status(eligibility, fee, now_ms)
                    next_fee_check = now + float(args.fee_check_seconds)

                if fee is None or not _zero_fee_confirmed(fee):
                    if position is None:
                        await asyncio.sleep(max(0.0, LOOP_SECONDS - (time.monotonic() - loop_started)))
                        continue

                if position is None:
                    if now < warmup_until or not lead.ready or not eligibility.can_open_new_position:
                        if now >= next_heartbeat:
                            fee_text = "?/?" if fee is None else f"{fee.maker}/{fee.taker}"
                            hybrid.console.print(
                                f"LEAD HEARTBEAT state={lead.reason} edge={lead.edge_bps:+.3f}bps "
                                f"Bmove={lead.binance_move_bps:+.3f}bps Mmove={lead.mexc_move_bps:+.3f}bps "
                                f"age={lead.age_ms:.0f}ms fee={fee_text}"
                            )
                            next_heartbeat = now + float(args.heartbeat_seconds)
                        await asyncio.sleep(max(0.0, LOOP_SECONDS - (time.monotonic() - loop_started)))
                        continue

                    direction = lead.direction
                    if (
                        latest_flow.direction == -direction
                        and latest_flow.confidence >= FLOW_VETO_CONFIDENCE
                    ):
                        if now >= next_heartbeat:
                            hybrid.console.print(
                                f"LEAD SKIP flow_disagreement edge={lead.edge_bps:+.3f}bps "
                                f"flow_dir={latest_flow.direction:+d} flow_conf={latest_flow.confidence:.3f}"
                            )
                            next_heartbeat = now + float(args.heartbeat_seconds)
                        await asyncio.sleep(max(0.0, LOOP_SECONDS - (time.monotonic() - loop_started)))
                        continue

                    side = OrderSide.LONG if direction == 1 else OrderSide.SHORT
                    ask = await adapter.get_best_price(symbol, OrderSide.LONG)
                    bid = await adapter.get_best_price(symbol, OrderSide.SHORT)
                    mid = (ask + bid) / 2.0
                    demo_spread_bps = ((ask - bid) / mid) * 10_000.0 if mid > 0 else 99999.0
                    required_edge = max(
                        float(args.min_edge_bps),
                        demo_spread_bps * float(args.edge_to_spread_ratio),
                    )

                    fresh_lead = model.snapshot(now_ms=int(time.time() * 1000))
                    if (
                        not fresh_lead.ready
                        or fresh_lead.direction != direction
                        or abs(fresh_lead.edge_bps) < required_edge
                    ):
                        await asyncio.sleep(max(0.0, LOOP_SECONDS - (time.monotonic() - loop_started)))
                        continue

                    # Re-check fee state immediately before the write if the cached
                    # fee observation is older than one second.
                    if now + 1.0 >= next_fee_check:
                        fee = await read_web_fee_status(adapter, symbol)
                        eligibility = apply_fee_status(eligibility, fee, int(time.time() * 1000))
                        next_fee_check = time.monotonic() + float(args.fee_check_seconds)
                    if not _zero_fee_confirmed(fee) or not eligibility.can_open_new_position:
                        continue

                    best_price = ask if side is OrderSide.LONG else bid
                    requested_qty = max(min_base_qty, target_notional / best_price)
                    signals_seen += 1
                    hybrid.console.print(
                        f"LEAD-LAG SIGNAL {'LONG' if side is OrderSide.LONG else 'SHORT'} "
                        f"edge={fresh_lead.edge_bps:+.3f}bps required={required_edge:.3f}bps "
                        f"Bmove={fresh_lead.binance_move_bps:+.3f}bps "
                        f"Mmove={fresh_lead.mexc_move_bps:+.3f}bps DemoSpread={demo_spread_bps:.3f}bps"
                    )
                    fill = await adapter.open_ioc(
                        symbol=symbol,
                        side=side,
                        price=best_price,
                        qty=requested_qty,
                        leverage=leverage,
                        client_order_id=f"leadlag-entry-{uuid.uuid4().hex}",
                    )
                    if fill.fee_usdt != 0:
                        raise MexcWebError(f"non-zero entry execution fee observed: {fill.fee_usdt}")

                    remote = await adapter.get_position(symbol)
                    if remote is None and (fill.order_id or fill.position_id or fill.filled_qty > 0):
                        remote = await _wait_remote_position(adapter, symbol, side)
                    if remote is None:
                        continue
                    if remote.side is not side:
                        raise MexcWebError("remote lead-lag position side mismatch")

                    position = remote
                    entry_side = side
                    entry_price = remote.entry_price or fill.avg_price or best_price
                    entry_fee = fill.fee_usdt
                    entry_time = time.monotonic()
                    entry_edge_bps = fresh_lead.edge_bps
                    mfe_bps = mae_bps = 0.0
                    exit_policy = _SpreadAwareExitPolicy(
                        side=1 if side is OrderSide.LONG else -1,
                        entry_price=entry_price,
                        early_adverse_changes=int(args.early_adverse_changes),
                        liq_buffer_fraction=float(args.liq_buffer_fraction),
                        winner_arm_bps=float(args.winner_arm_bps),
                        winner_pullback_bps=max(float(args.winner_pullback_bps), demo_spread_bps),
                        winner_flip_pullback_bps=max(0.5, demo_spread_bps * 0.5),
                        flip_confidence=float(args.exit_flip_confidence),
                        fade_confidence=float(args.exit_fade_confidence),
                        min_hold_seconds=float(args.min_hold_seconds),
                    )
                    next_position_price = 0.0
                    hybrid.console.print(
                        f"ENTRY {'LONG' if side is OrderSide.LONG else 'SHORT'} qty={remote.qty:g} "
                        f"entry={entry_price:g} edge={entry_edge_bps:+.3f}bps fee={entry_fee:g}"
                    )
                    continue

                assert position is not None
                assert entry_side is not None
                assert exit_policy is not None

                if now < next_position_price:
                    await asyncio.sleep(max(0.0, LOOP_SECONDS - (time.monotonic() - loop_started)))
                    continue
                next_position_price = now + POSITION_PRICE_SECONDS

                close_side = OrderSide.SHORT if entry_side is OrderSide.LONG else OrderSide.LONG
                executable_price = await adapter.get_best_price(symbol, close_side)
                last_executable_price = executable_price
                move_bps = hybrid._signed_move_bps(entry_side, entry_price, executable_price)
                mfe_bps = max(mfe_bps, move_bps)
                mae_bps = min(mae_bps, move_bps)

                policy_reason = exit_policy.on_tick(
                    price=executable_price,
                    liquidation_price=position.liquidation_price,
                    signal=latest_flow,
                    age_seconds=now - entry_time,
                    signal_fresh=flow_version != consumed_flow_version,
                )
                consumed_flow_version = flow_version

                direction = 1 if entry_side is OrderSide.LONG else -1
                current_lead = model.snapshot(now_ms=int(time.time() * 1000))
                convergence_level = max(
                    float(args.convergence_bps),
                    abs(entry_edge_bps) * float(args.convergence_fraction),
                )
                cross_reason: str | None = None
                if current_lead.age_ms <= float(args.max_quote_age_ms):
                    if abs(current_lead.edge_bps) <= convergence_level:
                        cross_reason = "lead_lag_converged"
                    elif (
                        current_lead.direction == -direction
                        and abs(current_lead.edge_bps) >= float(args.reversal_edge_bps)
                    ):
                        cross_reason = "lead_lag_reversed"
                    elif current_lead.binance_move_bps * direction <= -float(args.min_binance_move_bps):
                        cross_reason = "binance_reversal"
                if now - entry_time >= float(args.max_hold_seconds):
                    cross_reason = "lead_lag_timeout"

                reason = policy_reason or cross_reason
                if now >= next_heartbeat:
                    trail = exit_policy.trailing_stop_bps
                    trail_txt = "OFF" if trail is None else f"+{trail:.3f}bps"
                    hybrid.console.print(
                        f"POSITION mark={move_bps:+.3f}bps MFE={mfe_bps:+.3f}bps "
                        f"TRAIL={trail_txt} edge={current_lead.edge_bps:+.3f}bps "
                        f"Bmove={current_lead.binance_move_bps:+.3f}bps"
                    )
                    next_heartbeat = now + float(args.heartbeat_seconds)

                if reason is None:
                    await asyncio.sleep(max(0.0, LOOP_SECONDS - (time.monotonic() - loop_started)))
                    continue

                fill = await hybrid._flatten_position(adapter, position, reason)
                fees = entry_fee + fill.fee_usdt
                pnl_usdt, price_pct, roe_pct = _trade_pnl(
                    entry_side, entry_price, fill.avg_price, fill.filled_qty, leverage, fees
                )
                session_pnl += pnl_usdt
                peak_session_pnl = max(peak_session_pnl, session_pnl)
                max_drawdown_usdt = max(max_drawdown_usdt, peak_session_pnl - session_pnl)
                duration = now - entry_time
                if pnl_usdt > 0:
                    wins += 1
                    gross_profit += pnl_usdt
                elif pnl_usdt < 0:
                    losses += 1
                    gross_loss += abs(pnl_usdt)
                cycles += 1
                hybrid.console.print(
                    f"EXIT reason={reason} avg={fill.avg_price:g} fee={fill.fee_usdt:g}"
                )
                hybrid.console.print(
                    f"RESULT pnl={pnl_usdt:+.6f} USDT price={price_pct:+.4f}% ROE={roe_pct:+.2f}% "
                    f"MFE={mfe_bps:+.3f}bps MAE={mae_bps:+.3f}bps duration={duration:.2f}s "
                    f"session_pnl={session_pnl:+.6f}"
                )
                if fill.fee_usdt != 0:
                    raise MexcWebError(f"non-zero exit execution fee observed: {fill.fee_usdt}")
                position = None
                exit_policy = None
                entry_side = None
                entry_price = entry_fee = entry_time = entry_edge_bps = 0.0
                mfe_bps = mae_bps = 0.0

            if position is not None:
                fill = await hybrid._flatten_position(adapter, position, "session_timeout")
                side = entry_side or position.side
                fees = entry_fee + fill.fee_usdt
                pnl_usdt, price_pct, roe_pct = _trade_pnl(
                    side, entry_price, fill.avg_price, fill.filled_qty, leverage, fees
                )
                session_pnl += pnl_usdt
                peak_session_pnl = max(peak_session_pnl, session_pnl)
                max_drawdown_usdt = max(max_drawdown_usdt, peak_session_pnl - session_pnl)
                if pnl_usdt > 0:
                    wins += 1
                    gross_profit += pnl_usdt
                elif pnl_usdt < 0:
                    losses += 1
                    gross_loss += abs(pnl_usdt)
                cycles += 1
                hybrid.console.print(
                    f"TIMEOUT FLATTEN avg={fill.avg_price:g} pnl={pnl_usdt:+.6f} USDT ROE={roe_pct:+.2f}%"
                )
    finally:
        producer.cancel()
        try:
            await producer
        except asyncio.CancelledError:
            pass
        await binance_feed.close()

    pf = gross_profit / gross_loss if gross_loss > 0 else (float("inf") if gross_profit > 0 else 0.0)
    win_rate = wins / max(1, wins + losses) * 100.0
    hybrid.console.print(
        f"LEAD-LAG COMPLETE trades={cycles} signals={signals_seen} ticks={raw_ticks} wins={wins} losses={losses} "
        f"win_rate={win_rate:.1f}% PF={pf:.2f} total_pnl={session_pnl:+.6f} USDT "
        f"max_drawdown={max_drawdown_usdt:.6f} USDT"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Binance -> MEXC lead-lag Demo reconstruction")
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--session-seconds", type=int, default=1800)
    parser.add_argument("--max-cycles", type=int, default=50)
    parser.add_argument("--leverage", type=int, default=50)
    parser.add_argument("--target-margin-usdt", type=float, default=2.0)
    parser.add_argument("--lead-horizon-ms", type=int, default=250)
    parser.add_argument("--baseline-seconds", type=float, default=20.0)
    parser.add_argument("--warmup-seconds", type=float, default=8.0)
    parser.add_argument("--min-edge-bps", type=float, default=4.0)
    parser.add_argument("--min-binance-move-bps", type=float, default=1.0)
    parser.add_argument("--max-quote-age-ms", type=float, default=500.0)
    parser.add_argument("--edge-to-spread-ratio", type=float, default=1.15)
    parser.add_argument("--convergence-bps", type=float, default=0.75)
    parser.add_argument("--convergence-fraction", type=float, default=0.20)
    parser.add_argument("--reversal-edge-bps", type=float, default=1.5)
    parser.add_argument("--max-hold-seconds", type=float, default=90.0)
    parser.add_argument("--fee-check-seconds", type=float, default=5.0)
    parser.add_argument("--heartbeat-seconds", type=float, default=2.0)
    parser.add_argument("--early-adverse-changes", type=int, default=2)
    parser.add_argument("--winner-arm-bps", type=float, default=0.5)
    parser.add_argument("--winner-pullback-bps", type=float, default=1.5)
    parser.add_argument("--exit-flip-confidence", type=float, default=0.30)
    parser.add_argument("--exit-fade-confidence", type=float, default=0.12)
    parser.add_argument("--min-hold-seconds", type=float, default=0.35)
    parser.add_argument("--liq-buffer-fraction", type=float, default=0.25)
    args = parser.parse_args()
    try:
        asyncio.run(run(args))
    except MexcWebError as exc:
        hybrid.console.print(f"[red]LEAD-LAG DEMO FAILED:[/red] {exc}")
        raise SystemExit(2) from exc


if __name__ == "__main__":
    main()
