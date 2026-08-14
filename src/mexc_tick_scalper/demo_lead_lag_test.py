from __future__ import annotations

import argparse
import asyncio
import time
import uuid

from . import demo_hybrid_test as hybrid
from .demo_live_signal_test import _GuardedDemoAdapter, _as_pending_fill
from .demo_smoke import _assert_demo_safety
from .demo_tick_test import _trade_pnl
from .execution import OrderSide, PositionSnapshot
from .hybrid_strategy import MicrostructureSignal, MicrostructureSnapshot
from .lead_lag import BinanceBookTickerFeed, LeadLagModel
from .live_lead_lag_shadow import MexcBestBookFeed
from .market import MexcPublicMarket
from .state import EligibilityState, apply_fee_status
from .web_execution import MexcWebError, MexcWebExecutionAdapter, WebExecutionConfig
from .web_fee import read_web_fee_status

LIVE_REST = "https://contract.mexc.com"
LIVE_WS = "wss://contract.mexc.com/edge"
LOOP_SECONDS = 0.05
POSITION_PRICE_SECONDS = 0.10
FLOW_VETO_CONFIDENCE = 0.60


class _FastLeadLagDemoAdapter(_GuardedDemoAdapter):
    """TESTNET execution without REST LIVE lookups on the latency-critical path.

    LIVE market validation is performed by the continuously maintained Binance
    and MEXC websocket state in this module.  The inherited TESTNET position
    visibility and close-retry logic remains available, but entry no longer
    performs an additional public REST depth/ticker round trip.
    """

    async def get_best_price(self, symbol: str, side: OrderSide) -> float:
        return await MexcWebExecutionAdapter.get_best_price(self, symbol, side)

    async def open_ioc(self, *args, **kwargs):
        symbol = str(kwargs.get("symbol") or "").upper()
        side = kwargs.get("side")
        if not symbol or side not in (OrderSide.LONG, OrderSide.SHORT):
            raise MexcWebError("lead-lag IOC requires symbol and LONG/SHORT side")

        self._entry_started = True
        fill = await MexcWebExecutionAdapter.open_ioc(self, *args, **kwargs)
        if fill.filled_qty > 0:
            visible = await self._wait_position_visible(symbol, side)
            if visible is None:
                hybrid.console.print(
                    f"[yellow]IOC POSITION PENDING[/yellow] {symbol} "
                    f"reported_fill={fill.filled_qty:g}; continuing late-position reconciliation"
                )
                return _as_pending_fill(fill)
        return fill


async def _wait_remote_position(
    adapter: _FastLeadLagDemoAdapter,
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
    return (
        fee is not None
        and fee.maker is not None
        and fee.taker is not None
        and float(fee.maker) == 0.0
        and float(fee.taker) == 0.0
    )


def _required_live_edge(
    *,
    min_edge_bps: float,
    live_spread_bps: float,
    min_net_edge_bps: float,
    edge_to_spread_ratio: float,
) -> float:
    return max(
        float(min_edge_bps),
        float(live_spread_bps) + float(min_net_edge_bps),
        float(live_spread_bps) * float(edge_to_spread_ratio),
    )


def _fee_cache_allows_entry(
    fee,
    eligibility: EligibilityState,
    *,
    checked_at_ms: int,
    now_ms: int,
    max_age_ms: float,
) -> bool:
    if not _zero_fee_confirmed(fee) or not eligibility.can_open_new_position:
        return False
    if checked_at_ms <= 0:
        return False
    age = now_ms - checked_at_ms
    return 0 <= age <= float(max_age_ms)


async def run(args: argparse.Namespace) -> None:
    hybrid._load_project_env()
    symbol = args.symbol.upper()

    demo_cfg = WebExecutionConfig.demo_from_env(write_enabled=True)
    _assert_demo_safety(demo_cfg)
    live_fee_cfg = WebExecutionConfig.from_env(write_enabled=False)

    market = MexcPublicMarket(LIVE_REST, LIVE_WS)
    model = LeadLagModel(
        horizon_ms=args.lead_horizon_ms,
        baseline_seconds=args.baseline_seconds,
        min_edge_bps=args.min_edge_bps,
        min_binance_move_bps=args.min_binance_move_bps,
        max_age_ms=args.max_quote_age_ms,
    )
    binance_feed = BinanceBookTickerFeed(symbol, model)
    live_books = MexcBestBookFeed([symbol])
    flow = MicrostructureSignal(window_seconds=5.0, min_trade_rate=0.0)
    latest_flow = MicrostructureSnapshot(0, 0.0, 0.0, 0.5, 0.0, 0.0, 0)
    flow_version = 0
    consumed_flow_version = 0

    trade_queue: asyncio.Queue = asyncio.Queue(maxsize=20_000)

    async def pump_mexc_trades() -> None:
        async for tick in market.trades(symbol):
            if trade_queue.full():
                try:
                    trade_queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            trade_queue.put_nowait(tick)

    producer = asyncio.create_task(pump_mexc_trades())
    await binance_feed.start()
    await live_books.start()

    position: PositionSnapshot | None = None
    exit_policy: hybrid.AsymmetricExitPolicy | None = None
    entry_side: OrderSide | None = None
    entry_price = entry_demo_fee = entry_time = entry_edge_bps = 0.0
    entry_live_spread_bps = 0.0
    mfe_bps = mae_bps = 0.0
    cycles = wins = losses = signals_seen = raw_ticks = 0
    zero_fee_session_pnl = demo_reported_session_pnl = 0.0
    peak_zero_fee_pnl = max_drawdown_usdt = 0.0
    gross_profit = gross_loss = 0.0
    deadline = time.monotonic() + int(args.session_seconds)
    warmup_until = time.monotonic() + float(args.warmup_seconds)
    next_position_price = 0.0
    next_heartbeat = 0.0
    last_live_book_ts = 0
    live_fee = None
    live_fee_checked_at_ms = 0
    eligibility = EligibilityState(symbol)
    fee_stop = asyncio.Event()
    fee_task: asyncio.Task | None = None

    pending_side: OrderSide | None = None
    pending_edge_bps = 0.0
    pending_live_spread_bps = 0.0
    pending_demo_price = 0.0
    pending_until = 0.0
    next_pending_poll = 0.0

    hybrid.console.print(
        f"LIVE-LAG / DEMO EXEC {symbol}: Binance bookTicker=LIVE leader; MEXC depth/trades=LIVE lagger; "
        f"orders/positions=TESTNET only; edge>={args.min_edge_bps:.2f}bps "
        f"Bmove>={args.min_binance_move_bps:.2f}bps horizon={args.lead_horizon_ms}ms "
        f"loop={LOOP_SECONDS:.2f}s"
    )
    hybrid.console.print(
        "LIVE ZERO-FEE HARD GATE: new Demo IOC is allowed only while the REAL account fee cache "
        "confirms maker=0 and taker=0. Demo fee values do not define strategy eligibility."
    )
    hybrid.console.print(
        "LATENCY PATH: Binance WS + MEXC WS are local cached state; no LIVE REST depth/ticker call is made before IOC."
    )

    try:
        async with _FastLeadLagDemoAdapter(demo_cfg) as adapter, MexcWebExecutionAdapter(live_fee_cfg) as live_fee_adapter:
            async def monitor_live_fee() -> None:
                nonlocal live_fee, live_fee_checked_at_ms, eligibility
                while not fee_stop.is_set():
                    current = await read_web_fee_status(live_fee_adapter, symbol)
                    checked = int(time.time() * 1000)
                    live_fee = current
                    live_fee_checked_at_ms = checked
                    eligibility = apply_fee_status(eligibility, current, checked)
                    try:
                        await asyncio.wait_for(fee_stop.wait(), timeout=float(args.fee_check_seconds))
                    except TimeoutError:
                        pass

            fee_task = asyncio.create_task(monitor_live_fee())

            existing = await adapter.get_position(symbol)
            if existing is not None:
                raise MexcWebError(f"refusing lead-lag test: {symbol} already has open qty={existing.qty}")

            detail = await adapter.get_contract_detail(symbol)
            contract_size = float(detail.get("contractSize") or 0)
            min_vol = float(detail.get("minVol") or 0)
            max_leverage = int(detail.get("maxLeverage") or 1)
            if contract_size <= 0 or min_vol <= 0:
                raise MexcWebError("invalid TESTNET contract sizing metadata")
            min_base_qty = contract_size * min_vol
            leverage = min(max(1, int(args.leverage)), max_leverage)
            target_notional = max(0.01, float(args.target_margin_usdt)) * leverage

            while time.monotonic() < deadline and cycles < int(args.max_cycles):
                loop_started = time.monotonic()

                while True:
                    try:
                        tick = trade_queue.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                    raw_ticks += 1
                    latest_flow = flow.update(tick)
                    flow_version += 1

                now = time.monotonic()
                now_ms = int(time.time() * 1000)
                live_book = live_books.books.get(symbol)
                live_book_age_ms = float("inf")
                if live_book is not None:
                    live_book_age_ms = float(now_ms - live_book.ts_ms)
                    if live_book.ts_ms > last_live_book_ts and live_book_age_ms >= 0:
                        model.update_mexc(
                            bid=live_book.bid,
                            ask=live_book.ask,
                            ts_ms=live_book.ts_ms,
                        )
                        last_live_book_ts = live_book.ts_ms

                lead = model.snapshot(now_ms=now_ms)
                fee_ok = _fee_cache_allows_entry(
                    live_fee,
                    eligibility,
                    checked_at_ms=live_fee_checked_at_ms,
                    now_ms=now_ms,
                    max_age_ms=float(args.max_fee_age_seconds) * 1000.0,
                )

                if position is None and pending_side is not None:
                    if now >= next_pending_poll:
                        recovered = await adapter.get_position(symbol)
                        next_pending_poll = now + float(args.pending_poll_seconds)
                        if recovered is not None:
                            if recovered.side is not pending_side:
                                raise MexcWebError(
                                    f"late IOC side mismatch requested={pending_side.value} remote={recovered.side.value}"
                                )
                            position = recovered
                            entry_side = pending_side
                            entry_price = recovered.entry_price or pending_demo_price
                            entry_demo_fee = 0.0
                            entry_time = now - float(args.min_hold_seconds)
                            entry_edge_bps = pending_edge_bps
                            entry_live_spread_bps = pending_live_spread_bps
                            exit_policy = hybrid.AsymmetricExitPolicy(
                                side=1 if entry_side is OrderSide.LONG else -1,
                                entry_price=entry_price,
                                early_adverse_changes=int(args.early_adverse_changes),
                                liq_buffer_fraction=float(args.liq_buffer_fraction),
                                winner_arm_bps=float(args.winner_arm_bps),
                                winner_pullback_bps=max(float(args.winner_pullback_bps), entry_live_spread_bps),
                                winner_flip_pullback_bps=max(0.5, entry_live_spread_bps * 0.5),
                                flip_confidence=float(args.exit_flip_confidence),
                                fade_confidence=float(args.exit_fade_confidence),
                                min_hold_seconds=float(args.min_hold_seconds),
                            )
                            mfe_bps = mae_bps = 0.0
                            pending_side = None
                            hybrid.console.print(
                                f"[yellow]LATE DEMO POSITION RECOVERED[/yellow] {symbol} "
                                f"{'LONG' if entry_side is OrderSide.LONG else 'SHORT'} qty={position.qty:g} "
                                f"entry={entry_price:g}; managing instead of stacking"
                            )
                            continue
                    if pending_side is not None and now < pending_until:
                        await asyncio.sleep(max(0.0, LOOP_SECONDS - (time.monotonic() - loop_started)))
                        continue
                    if pending_side is not None:
                        hybrid.console.print(
                            f"[yellow]IOC UNCERTAINTY CLEARED[/yellow] {symbol}: no remote position appeared; resuming signals"
                        )
                        pending_side = None

                if position is None:
                    if (
                        now < warmup_until
                        or live_book is None
                        or live_book_age_ms < 0
                        or live_book_age_ms > float(args.max_live_book_age_ms)
                        or not lead.ready
                        or not fee_ok
                    ):
                        if now >= next_heartbeat:
                            fee_text = "?/?" if live_fee is None else f"{live_fee.maker}/{live_fee.taker}"
                            fee_age = "inf" if live_fee_checked_at_ms <= 0 else f"{max(0, now_ms-live_fee_checked_at_ms)}ms"
                            book_text = "missing" if live_book is None else f"{live_book_age_ms:.0f}ms/{live_book.spread_bps:.3f}bps"
                            state = lead.reason
                            if not fee_ok:
                                state = f"live_fee_gate:{eligibility.reason}"
                            elif live_book is None or live_book_age_ms > float(args.max_live_book_age_ms):
                                state = "live_book_stale"
                            hybrid.console.print(
                                f"LEAD HEARTBEAT state={state} edge={lead.edge_bps:+.3f}bps "
                                f"Bmove={lead.binance_move_bps:+.3f}bps Mmove={lead.mexc_move_bps:+.3f}bps "
                                f"quote_age={lead.age_ms:.0f}ms live_book={book_text} LIVEfee={fee_text} fee_age={fee_age}"
                            )
                            next_heartbeat = now + float(args.heartbeat_seconds)
                        await asyncio.sleep(max(0.0, LOOP_SECONDS - (time.monotonic() - loop_started)))
                        continue

                    direction = lead.direction
                    if latest_flow.direction == -direction and latest_flow.confidence >= FLOW_VETO_CONFIDENCE:
                        if now >= next_heartbeat:
                            hybrid.console.print(
                                f"LEAD SKIP flow_disagreement edge={lead.edge_bps:+.3f}bps "
                                f"flow_dir={latest_flow.direction:+d} flow_conf={latest_flow.confidence:.3f}"
                            )
                            next_heartbeat = now + float(args.heartbeat_seconds)
                        await asyncio.sleep(max(0.0, LOOP_SECONDS - (time.monotonic() - loop_started)))
                        continue

                    assert live_book is not None
                    live_spread_bps = live_book.spread_bps
                    required_edge = _required_live_edge(
                        min_edge_bps=float(args.min_edge_bps),
                        live_spread_bps=live_spread_bps,
                        min_net_edge_bps=float(args.min_net_edge_bps),
                        edge_to_spread_ratio=float(args.edge_to_spread_ratio),
                    )
                    fresh_lead = model.snapshot(now_ms=int(time.time() * 1000))
                    if (
                        not fresh_lead.ready
                        or fresh_lead.direction != direction
                        or abs(fresh_lead.edge_bps) < required_edge
                    ):
                        await asyncio.sleep(max(0.0, LOOP_SECONDS - (time.monotonic() - loop_started)))
                        continue

                    write_now_ms = int(time.time() * 1000)
                    if not _fee_cache_allows_entry(
                        live_fee,
                        eligibility,
                        checked_at_ms=live_fee_checked_at_ms,
                        now_ms=write_now_ms,
                        max_age_ms=float(args.max_fee_age_seconds) * 1000.0,
                    ):
                        continue

                    side = OrderSide.LONG if direction == 1 else OrderSide.SHORT
                    demo_entry_price = await adapter.get_best_price(symbol, side)
                    live_entry_price = live_book.ask if side is OrderSide.LONG else live_book.bid
                    divergence_bps = abs(demo_entry_price - live_entry_price) / live_entry_price * 10_000.0
                    if divergence_bps > float(args.max_demo_live_divergence_bps):
                        if now >= next_heartbeat:
                            hybrid.console.print(
                                f"LEAD SKIP demo_live_divergence={divergence_bps:.2f}bps > "
                                f"{float(args.max_demo_live_divergence_bps):.2f}bps"
                            )
                            next_heartbeat = now + float(args.heartbeat_seconds)
                        continue

                    requested_qty = max(min_base_qty, target_notional / demo_entry_price)
                    signals_seen += 1
                    hybrid.console.print(
                        f"LIVE-LAG SIGNAL {'LONG' if side is OrderSide.LONG else 'SHORT'} "
                        f"edge={fresh_lead.edge_bps:+.3f}bps required={required_edge:.3f}bps "
                        f"LIVEspread={live_spread_bps:.3f}bps Bmove={fresh_lead.binance_move_bps:+.3f}bps "
                        f"Mmove={fresh_lead.mexc_move_bps:+.3f}bps demo/live_div={divergence_bps:.2f}bps"
                    )
                    fill = await adapter.open_ioc(
                        symbol=symbol,
                        side=side,
                        price=demo_entry_price,
                        qty=requested_qty,
                        leverage=leverage,
                        client_order_id=f"live-lag-demo-entry-{uuid.uuid4().hex}",
                    )

                    remote = await adapter.get_position(symbol)
                    if remote is None and (fill.order_id or fill.position_id or fill.filled_qty > 0):
                        remote = await _wait_remote_position(adapter, symbol, side)
                    if remote is None:
                        if fill.order_id or fill.position_id or fill.filled_qty > 0:
                            pending_side = side
                            pending_edge_bps = fresh_lead.edge_bps
                            pending_live_spread_bps = live_spread_bps
                            pending_demo_price = demo_entry_price
                            pending_until = time.monotonic() + float(args.pending_reconcile_seconds)
                            next_pending_poll = 0.0
                            hybrid.console.print(
                                f"[yellow]IOC REMOTE POSITION UNCERTAIN[/yellow] {symbol}; "
                                f"blocking new entries for up to {float(args.pending_reconcile_seconds):g}s"
                            )
                        continue
                    if remote.side is not side:
                        raise MexcWebError("remote lead-lag position side mismatch")

                    try:
                        demo_ask, demo_bid = await asyncio.gather(
                            adapter.get_best_price(symbol, OrderSide.LONG),
                            adapter.get_best_price(symbol, OrderSide.SHORT),
                        )
                        demo_mid = (demo_ask + demo_bid) / 2.0
                        demo_spread_bps = ((demo_ask - demo_bid) / demo_mid) * 10_000.0 if demo_mid > 0 else live_spread_bps
                    except MexcWebError:
                        demo_spread_bps = live_spread_bps

                    position = remote
                    entry_side = side
                    entry_price = remote.entry_price or fill.avg_price or demo_entry_price
                    entry_demo_fee = fill.fee_usdt
                    entry_time = time.monotonic()
                    entry_edge_bps = fresh_lead.edge_bps
                    entry_live_spread_bps = live_spread_bps
                    mfe_bps = mae_bps = 0.0
                    exit_policy = hybrid.AsymmetricExitPolicy(
                        side=1 if side is OrderSide.LONG else -1,
                        entry_price=entry_price,
                        early_adverse_changes=int(args.early_adverse_changes),
                        liq_buffer_fraction=float(args.liq_buffer_fraction),
                        winner_arm_bps=float(args.winner_arm_bps),
                        winner_pullback_bps=max(float(args.winner_pullback_bps), demo_spread_bps, live_spread_bps),
                        winner_flip_pullback_bps=max(0.5, max(demo_spread_bps, live_spread_bps) * 0.5),
                        flip_confidence=float(args.exit_flip_confidence),
                        fade_confidence=float(args.exit_fade_confidence),
                        min_hold_seconds=float(args.min_hold_seconds),
                    )
                    next_position_price = 0.0
                    hybrid.console.print(
                        f"DEMO ENTRY {'LONG' if side is OrderSide.LONG else 'SHORT'} qty={remote.qty:g} "
                        f"entry={entry_price:g} LIVEedge={entry_edge_bps:+.3f}bps "
                        f"LIVEspread={entry_live_spread_bps:.3f}bps DemoFee={entry_demo_fee:g} "
                        f"(strategy fee=0 because LIVE fee gate is 0/0)"
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
                        cross_reason = "live_lead_lag_converged"
                    elif (
                        current_lead.direction == -direction
                        and abs(current_lead.edge_bps) >= float(args.reversal_edge_bps)
                    ):
                        cross_reason = "live_lead_lag_reversed"
                    elif current_lead.binance_move_bps * direction <= -float(args.min_binance_move_bps):
                        cross_reason = "live_binance_reversal"
                if now - entry_time >= float(args.max_hold_seconds):
                    cross_reason = "lead_lag_timeout"

                reason = policy_reason or cross_reason
                if now >= next_heartbeat:
                    trail = exit_policy.trailing_stop_bps
                    trail_txt = "OFF" if trail is None else f"+{trail:.3f}bps"
                    current_book = live_books.books.get(symbol)
                    current_live_spread = current_book.spread_bps if current_book is not None else float("nan")
                    hybrid.console.print(
                        f"DEMO POSITION mark={move_bps:+.3f}bps MFE={mfe_bps:+.3f}bps TRAIL={trail_txt} "
                        f"LIVEedge={current_lead.edge_bps:+.3f}bps LIVEspread={current_live_spread:.3f}bps "
                        f"Bmove={current_lead.binance_move_bps:+.3f}bps"
                    )
                    next_heartbeat = now + float(args.heartbeat_seconds)

                if reason is None:
                    await asyncio.sleep(max(0.0, LOOP_SECONDS - (time.monotonic() - loop_started)))
                    continue

                fill = await hybrid._flatten_position(adapter, position, reason)
                demo_fees = entry_demo_fee + fill.fee_usdt
                demo_pnl_usdt, price_pct, demo_roe_pct = _trade_pnl(
                    entry_side, entry_price, fill.avg_price, fill.filled_qty, leverage, demo_fees
                )
                zero_fee_pnl_usdt, _, zero_fee_roe_pct = _trade_pnl(
                    entry_side, entry_price, fill.avg_price, fill.filled_qty, leverage, 0.0
                )
                zero_fee_session_pnl += zero_fee_pnl_usdt
                demo_reported_session_pnl += demo_pnl_usdt
                peak_zero_fee_pnl = max(peak_zero_fee_pnl, zero_fee_session_pnl)
                max_drawdown_usdt = max(max_drawdown_usdt, peak_zero_fee_pnl - zero_fee_session_pnl)
                duration = now - entry_time
                if zero_fee_pnl_usdt > 0:
                    wins += 1
                    gross_profit += zero_fee_pnl_usdt
                elif zero_fee_pnl_usdt < 0:
                    losses += 1
                    gross_loss += abs(zero_fee_pnl_usdt)
                cycles += 1
                hybrid.console.print(
                    f"DEMO EXIT reason={reason} avg={fill.avg_price:g} DemoFee={fill.fee_usdt:g}"
                )
                hybrid.console.print(
                    f"RESULT zero_fee_pnl={zero_fee_pnl_usdt:+.6f} USDT zero_fee_ROE={zero_fee_roe_pct:+.2f}% "
                    f"demo_reported_pnl={demo_pnl_usdt:+.6f} USDT demo_ROE={demo_roe_pct:+.2f}% "
                    f"price={price_pct:+.4f}% MFE={mfe_bps:+.3f}bps MAE={mae_bps:+.3f}bps "
                    f"duration={duration:.2f}s zero_fee_session={zero_fee_session_pnl:+.6f}"
                )
                position = None
                exit_policy = None
                entry_side = None
                entry_price = entry_demo_fee = entry_time = entry_edge_bps = 0.0
                entry_live_spread_bps = 0.0
                mfe_bps = mae_bps = 0.0

            if position is not None:
                fill = await hybrid._flatten_position(adapter, position, "session_timeout")
                side = entry_side or position.side
                demo_fees = entry_demo_fee + fill.fee_usdt
                demo_pnl_usdt, _, demo_roe_pct = _trade_pnl(
                    side, entry_price, fill.avg_price, fill.filled_qty, leverage, demo_fees
                )
                zero_fee_pnl_usdt, _, zero_fee_roe_pct = _trade_pnl(
                    side, entry_price, fill.avg_price, fill.filled_qty, leverage, 0.0
                )
                zero_fee_session_pnl += zero_fee_pnl_usdt
                demo_reported_session_pnl += demo_pnl_usdt
                peak_zero_fee_pnl = max(peak_zero_fee_pnl, zero_fee_session_pnl)
                max_drawdown_usdt = max(max_drawdown_usdt, peak_zero_fee_pnl - zero_fee_session_pnl)
                if zero_fee_pnl_usdt > 0:
                    wins += 1
                    gross_profit += zero_fee_pnl_usdt
                elif zero_fee_pnl_usdt < 0:
                    losses += 1
                    gross_loss += abs(zero_fee_pnl_usdt)
                cycles += 1
                hybrid.console.print(
                    f"TIMEOUT FLATTEN zero_fee_pnl={zero_fee_pnl_usdt:+.6f} USDT "
                    f"zero_fee_ROE={zero_fee_roe_pct:+.2f}% demo_reported_pnl={demo_pnl_usdt:+.6f} "
                    f"demo_ROE={demo_roe_pct:+.2f}%"
                )
    finally:
        fee_stop.set()
        if fee_task is not None:
            try:
                await asyncio.wait_for(fee_task, timeout=1.0)
            except (TimeoutError, asyncio.CancelledError):
                fee_task.cancel()
                try:
                    await fee_task
                except asyncio.CancelledError:
                    pass
        producer.cancel()
        try:
            await producer
        except asyncio.CancelledError:
            pass
        await binance_feed.close()
        await live_books.close()

    pf = gross_profit / gross_loss if gross_loss > 0 else (float("inf") if gross_profit > 0 else 0.0)
    win_rate = wins / max(1, wins + losses) * 100.0
    hybrid.console.print(
        f"LIVE-LAG DEMO COMPLETE trades={cycles} signals={signals_seen} ticks={raw_ticks} "
        f"wins={wins} losses={losses} win_rate={win_rate:.1f}% PF={pf:.2f} "
        f"ZERO_FEE_PNL={zero_fee_session_pnl:+.6f} USDT "
        f"DEMO_REPORTED_PNL={demo_reported_session_pnl:+.6f} USDT "
        f"max_drawdown_zero_fee={max_drawdown_usdt:.6f} USDT"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="LIVE Binance -> LIVE MEXC lag signal with MEXC TESTNET execution")
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--session-seconds", type=int, default=1800)
    parser.add_argument("--max-cycles", type=int, default=50)
    parser.add_argument("--leverage", type=int, default=50)
    parser.add_argument("--target-margin-usdt", type=float, default=2.0)
    parser.add_argument("--lead-horizon-ms", type=int, default=250)
    parser.add_argument("--baseline-seconds", type=float, default=20.0)
    parser.add_argument("--warmup-seconds", type=float, default=8.0)
    parser.add_argument("--min-edge-bps", type=float, default=4.0)
    parser.add_argument("--min-net-edge-bps", type=float, default=2.0)
    parser.add_argument("--min-binance-move-bps", type=float, default=1.0)
    parser.add_argument("--max-quote-age-ms", type=float, default=500.0)
    parser.add_argument("--max-live-book-age-ms", type=float, default=500.0)
    parser.add_argument("--edge-to-spread-ratio", type=float, default=1.15)
    parser.add_argument("--max-demo-live-divergence-bps", type=float, default=35.0)
    parser.add_argument("--convergence-bps", type=float, default=0.75)
    parser.add_argument("--convergence-fraction", type=float, default=0.20)
    parser.add_argument("--reversal-edge-bps", type=float, default=1.5)
    parser.add_argument("--max-hold-seconds", type=float, default=90.0)
    parser.add_argument("--fee-check-seconds", type=float, default=5.0)
    parser.add_argument("--max-fee-age-seconds", type=float, default=8.0)
    parser.add_argument("--pending-reconcile-seconds", type=float, default=5.0)
    parser.add_argument("--pending-poll-seconds", type=float, default=0.15)
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
        hybrid.console.print(f"[red]LIVE-LAG DEMO FAILED:[/red] {exc}")
        raise SystemExit(2) from exc


if __name__ == "__main__":
    main()
