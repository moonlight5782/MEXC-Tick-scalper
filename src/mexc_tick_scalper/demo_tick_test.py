from __future__ import annotations

import argparse
import asyncio
import time

from rich.console import Console

from .config import load_config
from .demo_smoke import _assert_demo_safety
from .execution import OrderSide
from .market import MexcPublicMarket
from .models import FeeStatus
from .risk import PositionPlan
from .signals import momentum_direction
from .state import EligibilityState, apply_fee_status
from .trading import TradingController
from .web_execution import MexcWebError, MexcWebExecutionAdapter, WebExecutionConfig
from .web_fee import read_web_fee_status

console = Console()


def _pause_on_actual_fee(eligibility: EligibilityState, actual_fee_usdt: float, now_ms: int) -> EligibilityState:
    if actual_fee_usdt == 0:
        return eligibility
    return apply_fee_status(
        eligibility,
        FeeStatus(maker=0.0, taker=1.0, source=f"actual_execution_fee_usdt={actual_fee_usdt}"),
        now_ms,
    )


async def _wait_for_remote_position(adapter: MexcWebExecutionAdapter, symbol: str, timeout_seconds: float = 1.0):
    """Wait briefly for Demo open_positions to reflect an already-filled IOC order."""
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        position = await adapter.get_position(symbol)
        if position is not None:
            return position
        await asyncio.sleep(0.05)
    return None


def _trade_pnl(entry_side: OrderSide, entry_price: float, exit_price: float, qty: float, leverage: int, fees: float) -> tuple[float, float, float]:
    if entry_price <= 0 or qty <= 0:
        return 0.0, 0.0, 0.0
    direction = 1.0 if entry_side is OrderSide.LONG else -1.0
    price_return = direction * (exit_price - entry_price) / entry_price
    gross_pnl = direction * (exit_price - entry_price) * qty
    net_pnl = gross_pnl - fees
    margin = entry_price * qty / max(1, leverage)
    roe = net_pnl / margin if margin > 0 else 0.0
    return net_pnl, price_return * 100.0, roe * 100.0


async def run(args: argparse.Namespace) -> None:
    cfg = load_config(args.config)
    market_cfg = cfg.get("mexc", {})
    symbol = args.symbol.upper()
    momentum_ticks = max(1, int(args.momentum_ticks))
    reversal_ticks = max(1, int(args.reversal_ticks))

    web_cfg = WebExecutionConfig.demo_from_env(write_enabled=True)
    _assert_demo_safety(web_cfg)

    market = MexcPublicMarket(
        market_cfg.get("rest_base_url", "https://api.mexc.com"),
        market_cfg.get("websocket_url", "wss://contract.mexc.com/edge"),
    )

    async with MexcWebExecutionAdapter(web_cfg) as adapter:
        if await adapter.get_position(symbol) is not None:
            raise MexcWebError(f"refusing fast test: {symbol} already has an open position")

        fee = await read_web_fee_status(adapter, symbol)
        eligibility = apply_fee_status(EligibilityState(symbol), fee, int(time.time() * 1000))
        if not eligibility.can_open_new_position:
            raise MexcWebError(
                f"zero fee not confirmed: maker={fee.maker} taker={fee.taker} source={fee.source}"
            )

        detail = await adapter.get_contract_detail(symbol)
        contract_size = float(detail.get("contractSize") or 0)
        min_vol = float(detail.get("minVol") or 0)
        max_leverage = int(detail.get("maxLeverage") or 1)
        if contract_size <= 0 or min_vol <= 0:
            raise MexcWebError("invalid contract sizing metadata")

        qty = contract_size * min_vol * max(1, int(args.min_vol_multiplier))
        leverage = min(max(1, int(args.leverage)), max_leverage)
        controller = TradingController(adapter, reversal_ticks=reversal_ticks)
        prices: list[float] = []
        cycles = 0
        deadline = time.monotonic() + int(args.session_seconds)
        next_fee_check = 0.0
        entry_time: float | None = None
        entry_side: OrderSide | None = None
        entry_price: float | None = None
        entry_fee = 0.0
        session_pnl = 0.0

        console.print(
            f"FAST DEMO TICK TEST {symbol}: momentum={momentum_ticks} reversal={reversal_ticks} "
            f"qty={qty:g} leverage={leverage}x max_cycles={args.max_cycles}"
        )

        async for tick in market.trades(symbol):
            now_mono = time.monotonic()
            now_ms = int(time.time() * 1000)
            prices.append(tick.price)
            if len(prices) > max(32, momentum_ticks + 2):
                del prices[:-32]

            if now_mono >= next_fee_check:
                fee = await read_web_fee_status(adapter, symbol)
                eligibility = apply_fee_status(eligibility, fee, now_ms)
                next_fee_check = now_mono + float(args.fee_check_seconds)

            if symbol in controller.positions:
                closed = await controller.on_tick(tick)
                if closed:
                    fill = controller.last_exit_fill
                    if fill is None:
                        raise MexcWebError("exit triggered without observable fill")
                    console.print(
                        f"EXIT qty={fill.filled_qty:g} avg={fill.avg_price:g} fee={fill.fee_usdt:g}"
                    )
                    if entry_side is not None and entry_price is not None:
                        fees = entry_fee + fill.fee_usdt
                        pnl_usdt, price_pct, roe_pct = _trade_pnl(
                            entry_side, entry_price, fill.avg_price, fill.filled_qty, leverage, fees
                        )
                        duration = now_mono - entry_time if entry_time is not None else 0.0
                        session_pnl += pnl_usdt
                        console.print(
                            f"RESULT pnl={pnl_usdt:+.6f} USDT price={price_pct:+.4f}% "
                            f"ROE={roe_pct:+.2f}% duration={duration:.2f}s session_pnl={session_pnl:+.6f}"
                        )
                    eligibility = _pause_on_actual_fee(eligibility, fill.fee_usdt, now_ms)
                    cycles += 1
                    entry_time = None
                    entry_side = None
                    entry_price = None
                    entry_fee = 0.0
                    remote = await controller.reconcile(symbol)
                    if remote is not None:
                        raise MexcWebError(f"position remains after exit: qty={remote.qty}")
                    if cycles >= int(args.max_cycles):
                        break
                continue

            if not eligibility.can_open_new_position:
                if now_mono >= deadline:
                    break
                continue

            direction = momentum_direction(prices, momentum_ticks)
            if direction == 0:
                if now_mono >= deadline:
                    break
                continue

            side = OrderSide.LONG if direction == 1 else OrderSide.SHORT
            best_price = await adapter.get_best_price(symbol, side)
            notional = qty * best_price
            plan = PositionPlan(
                bankroll_usdt=0.0,
                margin_usdt=notional / leverage,
                leverage=leverage,
                target_notional_usdt=notional,
                qty=qty,
                confidence=1.0,
            )
            opened = await controller.open_from_signal(
                symbol=symbol,
                direction=direction,
                best_price=best_price,
                plan=plan,
                eligibility=eligibility,
            )
            if opened:
                fill = controller.last_entry_fill
                if fill is None:
                    raise MexcWebError("entry opened without observable fill")
                console.print(
                    f"ENTRY {'LONG' if direction == 1 else 'SHORT'} qty={fill.filled_qty:g} "
                    f"avg={fill.avg_price:g} fee={fill.fee_usdt:g}"
                )
                entry_time = now_mono
                entry_side = side
                entry_price = fill.avg_price
                entry_fee = fill.fee_usdt
                eligibility = _pause_on_actual_fee(eligibility, fill.fee_usdt, now_ms)

                remote = await _wait_for_remote_position(adapter, symbol)
                if remote is None:
                    controller.positions.pop(symbol, None)
                    raise MexcWebError(
                        f"IOC fill reported for {symbol}, but position did not become visible in open_positions within 1s"
                    )
                managed = controller.positions.get(symbol)
                if managed is not None:
                    managed.snapshot = remote
                console.print(
                    f"POSITION CONFIRMED qty={remote.qty:g} entry={remote.entry_price:g} "
                    f"liq={remote.liquidation_price if remote.liquidation_price is not None else '?'}"
                )

            if now_mono >= deadline:
                break

        if symbol in controller.positions:
            managed = controller.positions[symbol]
            close_side = OrderSide.SHORT if managed.snapshot.side is OrderSide.LONG else OrderSide.LONG
            fill = await adapter.close_market_reduce_only(
                symbol=symbol,
                qty=managed.snapshot.qty,
                side=close_side,
                client_order_id=f"fast-timeout-{int(time.time() * 1000)}",
            )
            console.print(
                f"TIMEOUT FLATTEN qty={fill.filled_qty:g} avg={fill.avg_price:g} fee={fill.fee_usdt:g}"
            )
            if entry_side is not None and entry_price is not None:
                fees = entry_fee + fill.fee_usdt
                pnl_usdt, price_pct, roe_pct = _trade_pnl(
                    entry_side, entry_price, fill.avg_price, fill.filled_qty, leverage, fees
                )
                duration = time.monotonic() - entry_time if entry_time is not None else 0.0
                session_pnl += pnl_usdt
                console.print(
                    f"RESULT pnl={pnl_usdt:+.6f} USDT price={price_pct:+.4f}% "
                    f"ROE={roe_pct:+.2f}% duration={duration:.2f}s session_pnl={session_pnl:+.6f}"
                )
            await controller.reconcile(symbol)

        console.print(
            f"[green]FAST DEMO TICK TEST COMPLETE[/green] cycles={cycles} session_pnl={session_pnl:+.6f} USDT"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="Fast MEXC Demo tick entry/exit diagnostic")
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--session-seconds", type=int, default=120)
    parser.add_argument("--max-cycles", type=int, default=3)
    parser.add_argument("--momentum-ticks", type=int, default=3)
    parser.add_argument("--reversal-ticks", type=int, default=1)
    parser.add_argument("--leverage", type=int, default=5)
    parser.add_argument("--min-vol-multiplier", type=int, default=1)
    parser.add_argument("--fee-check-seconds", type=float, default=15.0)
    args = parser.parse_args()
    try:
        asyncio.run(run(args))
    except MexcWebError as exc:
        console.print(f"[red]FAST DEMO TICK TEST FAILED:[/red] {exc}")
        raise SystemExit(2) from exc


if __name__ == "__main__":
    main()
