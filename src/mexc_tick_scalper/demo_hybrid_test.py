from __future__ import annotations

import argparse
import asyncio
import time
import uuid
from pathlib import Path

from dotenv import load_dotenv
from rich.console import Console

from .config import load_config
from .demo_smoke import _assert_demo_safety
from .demo_tick_test import _trade_pnl, _wait_for_remote_position
from .execution import OrderFill, OrderSide, PositionSnapshot
from .hybrid_strategy import AsymmetricExitPolicy, MicrostructureSignal
from .market import MexcPublicMarket
from .state import EligibilityState, apply_fee_status
from .web_execution import MexcWebError, MexcWebExecutionAdapter, WebExecutionConfig
from .web_fee import read_web_fee_status

console = Console()


def _load_project_env() -> None:
    project_root = Path(__file__).resolve().parents[2]
    load_dotenv(project_root / ".env", override=False)


async def _close_confirmed(
    adapter: MexcWebExecutionAdapter,
    position: PositionSnapshot,
    reason: str,
) -> OrderFill:
    close_side = OrderSide.SHORT if position.side is OrderSide.LONG else OrderSide.LONG
    fill = await adapter.close_market_reduce_only(
        symbol=position.symbol,
        qty=position.qty,
        side=close_side,
        client_order_id=f"hybrid-exit-{uuid.uuid4().hex}",
    )

    deadline = time.monotonic() + 1.0
    remote = position
    while time.monotonic() < deadline:
        current = await adapter.get_position(position.symbol)
        if current is None:
            return OrderFill(
                symbol=fill.symbol,
                side=fill.side,
                requested_qty=fill.requested_qty,
                filled_qty=position.qty,
                avg_price=fill.avg_price,
                fee_usdt=fill.fee_usdt,
                order_id=fill.order_id,
                client_order_id=fill.client_order_id,
                position_id=fill.position_id,
            )
        remote = current
        if current.qty < position.qty - 1e-12:
            closed_qty = position.qty - current.qty
            return OrderFill(
                symbol=fill.symbol,
                side=fill.side,
                requested_qty=fill.requested_qty,
                filled_qty=closed_qty,
                avg_price=fill.avg_price,
                fee_usdt=fill.fee_usdt,
                order_id=fill.order_id,
                client_order_id=fill.client_order_id,
                position_id=fill.position_id,
            )
        await asyncio.sleep(0.05)

    raise MexcWebError(f"{reason}: close was submitted but position is still open qty={remote.qty}")


async def run(args: argparse.Namespace) -> None:
    _load_project_env()
    cfg = load_config(args.config)
    symbol = args.symbol.upper()

    web_cfg = WebExecutionConfig.demo_from_env(write_enabled=True)
    _assert_demo_safety(web_cfg)

    # Demo execution, order book and trade tape must come from the same MEXC
    # testnet environment. Using the live contract websocket here creates false
    # or irrelevant signals for demo-only symbols.
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

        position: PositionSnapshot | None = None
        exit_policy: AsymmetricExitPolicy | None = None
        entry_side: OrderSide | None = None
        entry_price = 0.0
        entry_fee = 0.0
        entry_time = 0.0
        session_pnl = 0.0
        gross_profit = 0.0
        gross_loss = 0.0
        wins = 0
        losses = 0
        cycles = 0
        signals_seen = 0
        raw_ticks = 0
        deadline = time.monotonic() + int(args.session_seconds)
        next_fee_check = 0.0
        next_heartbeat = 0.0

        console.print(
            f"HYBRID DEMO {symbol}: target_margin={target_margin:g} USDT target_notional={target_notional:g} USDT "
            f"leverage={leverage}x confidence>={args.min_confidence:.2f} early_adverse={args.early_adverse_changes} "
            f"liq_buffer={args.liq_buffer_fraction:.0%}"
        )
        console.print("SCANNING: waiting for MEXC TESTNET trade ticks...")

        async for tick in market.trades(symbol):
            raw_ticks += 1
            now = time.monotonic()
            now_ms = int(time.time() * 1000)
            snap = signal.update(tick)

            if now >= next_fee_check:
                fee = await read_web_fee_status(adapter, symbol)
                eligibility = apply_fee_status(eligibility, fee, now_ms)
                next_fee_check = now + float(args.fee_check_seconds)

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
                try:
                    ask_hb = await adapter.get_best_price(symbol, OrderSide.LONG)
                    bid_hb = await adapter.get_best_price(symbol, OrderSide.SHORT)
                    mid_hb = (ask_hb + bid_hb) / 2.0
                    spread_bps = ((ask_hb - bid_hb) / mid_hb) * 10_000 if mid_hb > 0 else 99999.0
                    spread_txt = f"{spread_bps:.3f}"
                    if spread_bps > float(args.max_spread_bps):
                        blockers.append("spread")
                except MexcWebError:
                    blockers.append("book")
                state = "IN_POSITION" if position is not None else ("READY" if not blockers else "BLOCKED:" + ",".join(blockers))
                console.print(
                    f"HEARTBEAT ticks={raw_ticks} rate={snap.trade_rate:.2f}/s confidence={snap.confidence:.3f} "
                    f"direction={snap.direction:+d} price_changes={snap.price_changes} spread={spread_txt}bps "
                    f"fee={fee.maker}/{fee.taker} state={state}"
                )
                next_heartbeat = now + float(args.heartbeat_seconds)

            if position is not None:
                assert exit_policy is not None
                reason = exit_policy.on_tick(
                    price=tick.price,
                    liquidation_price=position.liquidation_price,
                    signal=snap,
                    age_seconds=now - entry_time,
                )
                if reason is not None:
                    fill = await _close_confirmed(adapter, position, reason)
                    fees = entry_fee + fill.fee_usdt
                    pnl_usdt, price_pct, roe_pct = _trade_pnl(
                        entry_side or position.side,
                        entry_price,
                        fill.avg_price,
                        fill.filled_qty,
                        leverage,
                        fees,
                    )
                    session_pnl += pnl_usdt
                    duration = now - entry_time
                    if pnl_usdt > 0:
                        wins += 1
                        gross_profit += pnl_usdt
                    elif pnl_usdt < 0:
                        losses += 1
                        gross_loss += abs(pnl_usdt)
                    console.print(f"EXIT reason={reason} qty={fill.filled_qty:g} avg={fill.avg_price:g} fee={fill.fee_usdt:g}")
                    console.print(
                        f"RESULT pnl={pnl_usdt:+.6f} USDT price={price_pct:+.4f}% ROE={roe_pct:+.2f}% "
                        f"duration={duration:.2f}s session_pnl={session_pnl:+.6f}"
                    )
                    cycles += 1
                    position = None
                    exit_policy = None
                    entry_side = None
                    entry_price = 0.0
                    entry_fee = 0.0
                    if fill.fee_usdt != 0:
                        raise MexcWebError(f"non-zero execution fee observed on exit: {fill.fee_usdt}")
                    if cycles >= int(args.max_cycles):
                        break
                if now >= deadline:
                    break
                continue

            if now >= deadline:
                break
            if not eligibility.can_open_new_position:
                continue
            if snap.direction == 0 or snap.confidence < float(args.min_confidence):
                continue
            if snap.price_changes < int(args.min_price_changes):
                continue

            signals_seen += 1
            side = OrderSide.LONG if snap.direction == 1 else OrderSide.SHORT
            ask = await adapter.get_best_price(symbol, OrderSide.LONG)
            bid = await adapter.get_best_price(symbol, OrderSide.SHORT)
            mid = (ask + bid) / 2.0
            spread_bps = ((ask - bid) / mid) * 10_000 if mid > 0 else 99999.0
            if spread_bps > float(args.max_spread_bps):
                continue
            best_price = ask if side is OrderSide.LONG else bid

            requested_qty = max(min_base_qty, target_notional / best_price)
            fill = await adapter.open_ioc(
                symbol=symbol,
                side=side,
                price=best_price,
                qty=requested_qty,
                leverage=leverage,
                client_order_id=f"hybrid-entry-{uuid.uuid4().hex}",
            )
            if fill.filled_qty <= 0:
                continue
            if fill.fee_usdt != 0:
                raise MexcWebError(f"non-zero execution fee observed on entry: {fill.fee_usdt}")

            remote = await _wait_for_remote_position(adapter, symbol)
            if remote is None:
                raise MexcWebError("IOC fill reported but position did not appear in open_positions within 1s")

            position = remote
            entry_side = side
            entry_price = remote.entry_price or fill.avg_price
            entry_fee = fill.fee_usdt
            entry_time = now
            exit_policy = AsymmetricExitPolicy(
                side=1 if side is OrderSide.LONG else -1,
                entry_price=entry_price,
                early_adverse_changes=int(args.early_adverse_changes),
                liq_buffer_fraction=float(args.liq_buffer_fraction),
                winner_arm_bps=float(args.winner_arm_bps),
                winner_pullback_bps=float(args.winner_pullback_bps),
                flip_confidence=float(args.exit_flip_confidence),
                fade_confidence=float(args.exit_fade_confidence),
                min_hold_seconds=float(args.min_hold_seconds),
            )
            fill_ratio = fill.filled_qty / requested_qty if requested_qty > 0 else 0.0
            console.print(
                f"ENTRY {'LONG' if side is OrderSide.LONG else 'SHORT'} requested={requested_qty:g} "
                f"filled={remote.qty:g} fill_ratio={fill_ratio:.1%} entry={entry_price:g} "
                f"liq={remote.liquidation_price if remote.liquidation_price is not None else '?'} fee={fill.fee_usdt:g}"
            )
            console.print(
                f"SIGNAL confidence={snap.confidence:.3f} momentum={snap.momentum_bps:+.3f}bps "
                f"CVD={snap.cvd_norm:+.3f} buy_ratio={snap.buy_ratio:.3f} rate={snap.trade_rate:.2f}/s "
                f"spread={spread_bps:.3f}bps"
            )

        if position is not None:
            fill = await _close_confirmed(adapter, position, "session_timeout")
            fees = entry_fee + fill.fee_usdt
            pnl_usdt, price_pct, roe_pct = _trade_pnl(
                entry_side or position.side, entry_price, fill.avg_price, fill.filled_qty, leverage, fees
            )
            session_pnl += pnl_usdt
            console.print(f"TIMEOUT FLATTEN qty={fill.filled_qty:g} avg={fill.avg_price:g} fee={fill.fee_usdt:g}")
            console.print(
                f"RESULT pnl={pnl_usdt:+.6f} USDT price={price_pct:+.4f}% ROE={roe_pct:+.2f}% "
                f"session_pnl={session_pnl:+.6f}"
            )

        pf = gross_profit / gross_loss if gross_loss > 0 else (float('inf') if gross_profit > 0 else 0.0)
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
    args = parser.parse_args()
    try:
        asyncio.run(run(args))
    except MexcWebError as exc:
        console.print(f"[red]HYBRID DEMO FAILED:[/red] {exc}")
        raise SystemExit(2) from exc


if __name__ == "__main__":
    main()
