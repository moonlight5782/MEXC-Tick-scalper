from __future__ import annotations

import argparse
import asyncio
import time

from rich.console import Console

from .backtest import walk_forward
from .config import load_config
from .demo_smoke import _assert_demo_safety
from .execution import OrderSide
from .market import MexcPublicMarket
from .risk import PositionPlan
from .signals import momentum_direction
from .state import EligibilityState, apply_fee_status
from .trading import TradingController
from .web_execution import MexcWebError, MexcWebExecutionAdapter, WebExecutionConfig
from .web_fee import read_web_fee_status

console = Console()


async def _warmup_ticks(market: MexcPublicMarket, symbol: str, seconds: int):
    ticks = []
    deadline = time.monotonic() + seconds
    async for tick in market.trades(symbol):
        ticks.append(tick)
        if time.monotonic() >= deadline:
            break
    return ticks


async def run(args: argparse.Namespace) -> None:
    cfg = load_config(args.config)
    strategy_cfg = cfg.get("strategy", {})
    market_cfg = cfg.get("mexc", {})
    symbol = args.symbol.upper()

    web_cfg = WebExecutionConfig.from_env(base_url=args.base_url, write_enabled=True)
    _assert_demo_safety(web_cfg.base_url)

    market = MexcPublicMarket(
        market_cfg.get("rest_base_url", "https://api.mexc.com"),
        market_cfg.get("websocket_url", "wss://contract.mexc.com/edge"),
    )

    console.print(f"Warmup {symbol}: collecting {args.warmup_seconds}s of fresh ticks...")
    warmup = await _warmup_ticks(market, symbol, int(args.warmup_seconds))
    wf = walk_forward(
        symbol=symbol,
        ticks=warmup,
        momentum_grid=[int(x) for x in strategy_cfg.get("momentum_ticks", [2, 3, 4, 5, 6])],
        reversal_grid=[int(x) for x in strategy_cfg.get("reversal_ticks", [1, 2])],
        max_hold_seconds=int(strategy_cfg.get("max_hold_seconds", 180)),
        min_train_trades=max(2, int(args.min_train_trades)),
        train_fraction=float(args.train_fraction),
    )
    if wf is None:
        raise MexcWebError("warmup produced insufficient trades for walk-forward selection")

    min_pf = float(strategy_cfg.get("min_profit_factor", 1.30))
    min_exp = float(strategy_cfg.get("min_expectancy_bps", 0.0))
    validation = wf.validation
    if validation.trades <= 0 or validation.profit_factor < min_pf or validation.expectancy_bps <= min_exp:
        raise MexcWebError(
            f"walk-forward edge rejected: trades={validation.trades} "
            f"PF={validation.profit_factor:.3f} expectancy={validation.expectancy_bps:.4f}bps"
        )

    momentum_ticks = wf.train.momentum_ticks
    reversal_ticks = wf.train.reversal_ticks
    console.print(
        f"[green]Walk-forward passed[/green]: momentum={momentum_ticks} reversal={reversal_ticks} "
        f"validation trades={validation.trades} PF={validation.profit_factor:.2f} "
        f"expectancy={validation.expectancy_bps:.3f}bps"
    )

    async with MexcWebExecutionAdapter(web_cfg) as adapter:
        existing = await adapter.get_position(symbol)
        if existing is not None:
            raise MexcWebError(f"refusing Demo session: {symbol} already has an open position")

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
        prices: list[float] = [tick.price for tick in warmup[-(momentum_ticks + 1):]]
        cycles = 0
        next_fee_check = time.monotonic()
        session_deadline = time.monotonic() + int(args.session_seconds)
        cooldown_until_ms = 0

        console.print(
            f"Starting guarded Demo tick session: qty={qty:g} base units, leverage={leverage}x, "
            f"max_cycles={args.max_cycles}"
        )

        async for tick in market.trades(symbol):
            now_mono = time.monotonic()
            now_ms = int(time.time() * 1000)
            prices.append(tick.price)
            if len(prices) > max(64, momentum_ticks + 2):
                del prices[:-64]

            if now_mono >= next_fee_check:
                fee = await read_web_fee_status(adapter, symbol)
                previous = eligibility.status
                eligibility = apply_fee_status(eligibility, fee, now_ms)
                if eligibility.status != previous:
                    console.print(f"Fee state -> {eligibility.status.value}: {eligibility.reason}")
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
                    if fill.fee_usdt != 0:
                        eligibility = apply_fee_status(
                            eligibility,
                            await read_web_fee_status(adapter, symbol),
                            now_ms,
                        )
                        console.print("[red]Non-zero execution fee observed; new entries paused.[/red]")
                    cycles += 1
                    cooldown_until_ms = now_ms + int(strategy_cfg.get("cooldown_after_exit_ms", 250))
                    remote = await controller.reconcile(symbol)
                    if remote is not None:
                        raise MexcWebError(f"position remains after exit: qty={remote.qty}")
                    if cycles >= int(args.max_cycles):
                        break
                continue

            if now_ms < cooldown_until_ms or not eligibility.can_open_new_position:
                if now_mono >= session_deadline:
                    break
                continue

            direction = momentum_direction(prices, momentum_ticks)
            if direction == 0:
                if now_mono >= session_deadline:
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
                if fill.fee_usdt != 0:
                    eligibility = apply_fee_status(
                        eligibility,
                        await read_web_fee_status(adapter, symbol),
                        now_ms,
                    )
                    console.print("[red]Non-zero entry fee observed; further entries paused.[/red]")

            if now_mono >= session_deadline:
                break

        if symbol in controller.positions:
            raise MexcWebError(
                "Demo session ended with an open position; keep the process running and close it manually in Demo"
            )
        console.print(f"[green]DEMO TICK SESSION COMPLETE[/green] cycles={cycles}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Adaptive guarded MEXC Demo tick session")
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--base-url", default=None)
    parser.add_argument("--warmup-seconds", type=int, default=180)
    parser.add_argument("--session-seconds", type=int, default=600)
    parser.add_argument("--max-cycles", type=int, default=5)
    parser.add_argument("--leverage", type=int, default=5)
    parser.add_argument("--min-vol-multiplier", type=int, default=1)
    parser.add_argument("--fee-check-seconds", type=float, default=30.0)
    parser.add_argument("--train-fraction", type=float, default=0.70)
    parser.add_argument("--min-train-trades", type=int, default=5)
    args = parser.parse_args()
    try:
        asyncio.run(run(args))
    except MexcWebError as exc:
        console.print(f"[red]DEMO SESSION FAILED:[/red] {exc}")
        raise SystemExit(2) from exc


if __name__ == "__main__":
    main()
