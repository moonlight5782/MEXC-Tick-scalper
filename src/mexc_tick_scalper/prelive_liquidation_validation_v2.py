from __future__ import annotations

import asyncio
import math
import time
from pathlib import Path

from rich.console import Console

from . import prelive_liquidation_validation as base
from . import prelive_persistent_ioc_shadow_v2 as v2
from .baseline_v1 import apply_baseline_v1
from .margin_liquidation_replay import fetch_contract_risk, liquidation_distance_bps, liquidation_price

console = Console()


class BankDepleted(RuntimeError):
    pass


async def run(args) -> None:
    apply_baseline_v1(args)
    target = max(1, int(args.target_closed_trades))
    symbols = base._selected_symbols(args)
    risks = await fetch_contract_risk()

    stamp = time.strftime("%Y%m%d_%H%M%S")
    fair_csv = Path(args.fair_csv or f"fair_price_trace_{stamp}.csv")
    report_csv = Path(args.report_csv or f"liquidation_validation_{stamp}.csv")

    console.print("[bold cyan]BANK-AWARE BASELINE V1 + REAL MEXC FAIR-PRICE LIQUIDATION TEST[/bold cyan]")
    console.print(
        f"NO REAL ORDERS. start_bank=${args.balance_usdt:.2f}; margin_fraction={args.margin_fraction:.0%}; "
        f"leverage={'MEXC_MAX' if args.leverage <= 0 else str(args.leverage)+'x'}; target_closed={target}"
    )
    console.print("IOC entry size is capped BEFORE walking the live arrival-time MEXC book.")
    console.print(f"Fair-price trace: {fair_csv}")
    console.print(f"Liquidation report: {report_csv}")

    fair_rows: list[base.FairTick] = []
    closed_trades: list[base.ClosedTrade] = []
    fair_stop = asyncio.Event()
    fair_task = asyncio.create_task(base._fair_price_loop(symbols, fair_stop, fair_rows, fair_csv))

    sim_balance = float(args.balance_usdt)
    current_entry_symbol: str | None = None

    original_close = v2._close_trade
    original_delayed = v2.delayed_catchup_entry_ok
    original_fill = v2.virtual_ioc_fill

    def delayed_with_symbol(*call_args, **call_kwargs):
        nonlocal current_entry_symbol
        signal = call_kwargs.get("signal")
        if signal is not None:
            current_entry_symbol = signal.symbol
        return original_delayed(*call_args, **call_kwargs)

    def bank_capped_fill(book, *, direction: int, target_notional_usdt: float, contract_size: float, cross_bps: float):
        symbol = current_entry_symbol
        if symbol is None or symbol not in risks:
            return original_fill(
                book,
                direction=direction,
                target_notional_usdt=target_notional_usdt,
                contract_size=contract_size,
                cross_bps=cross_bps,
            )
        risk = risks[symbol]
        leverage = risk.max_leverage if args.leverage <= 0 else min(args.leverage, risk.max_leverage)
        leverage = max(1, leverage)
        bank_cap = max(0.0, sim_balance) * args.margin_fraction * leverage
        capped_target = min(float(target_notional_usdt), bank_cap)
        return original_fill(
            book,
            direction=direction,
            target_notional_usdt=capped_target,
            contract_size=contract_size,
            cross_bps=cross_bps,
        )

    def close_capture(stats: v2.Stats, pos: v2.Position, now_ms: int) -> None:
        nonlocal sim_balance
        logged_bps = pos.realized_pnl_usdt / max(pos.entry_notional, 1e-12) * 10_000.0
        trade = base.ClosedTrade(
            index=len(closed_trades) + 1,
            symbol=pos.signal.symbol,
            direction=pos.signal.direction,
            entry_ts_ms=pos.entry_ts_ms,
            exit_ts_ms=now_ms,
            entry_price=pos.entry_price,
            recorded_notional=pos.entry_notional,
            logged_pnl_usdt=pos.realized_pnl_usdt,
            logged_pnl_bps=logged_bps,
            exit_reason=pos.exit_reason or "unknown",
        )
        closed_trades.append(trade)

        risk = risks.get(trade.symbol)
        leverage = 1
        sim_pnl = pos.realized_pnl_usdt
        liquidated = False
        fair_count = 0
        adverse_bps = math.nan
        if risk is not None:
            leverage = risk.max_leverage if args.leverage <= 0 else min(args.leverage, risk.max_leverage)
            leverage = max(1, leverage)
            ticks_by_symbol: dict[str, list[base.FairTick]] = {}
            for tick in fair_rows:
                if tick.symbol == trade.symbol:
                    ticks_by_symbol.setdefault(tick.symbol, []).append(tick)
            path = base._fair_path_for_trade(ticks_by_symbol, trade)
            fair_count = len(path)
            if path:
                liq_px = liquidation_price(
                    trade.entry_price,
                    trade.direction,
                    leverage,
                    risk.maintenance_margin_rate,
                    args.liquidation_fee_rate,
                )
                adverse = min(t.price for t in path) if trade.direction > 0 else max(t.price for t in path)
                adverse_bps = trade.direction * (adverse / trade.entry_price - 1.0) * 10_000.0
                liquidated = adverse <= liq_px if trade.direction > 0 else adverse >= liq_px
                if liquidated:
                    liq_dist = liquidation_distance_bps(
                        leverage, risk.maintenance_margin_rate, args.liquidation_fee_rate
                    )
                    sim_pnl = -trade.recorded_notional * liq_dist / 10_000.0

        sim_balance = max(0.0, sim_balance + sim_pnl)
        original_close(stats, pos, now_ms)
        adverse_text = "n/a" if math.isnan(adverse_bps) else f"{adverse_bps:+.2f}bps"
        console.print(
            f"[bold]BANK[/bold] #{trade.index} {trade.symbol} {leverage}x "
            f"notional=${trade.recorded_notional:.0f} fair_ticks={fair_count} adverse={adverse_text} "
            f"{'[red]LIQUIDATED[/red]' if liquidated else 'survived'} sim_pnl=${sim_pnl:+.2f} balance=${sim_balance:.2f}"
        )

        if sim_balance <= 0:
            raise BankDepleted("simulated bank reached zero")
        if len(closed_trades) >= target:
            raise base.TargetClosedTradesReached

    v2._close_trade = close_capture
    v2.delayed_catchup_entry_ok = delayed_with_symbol
    v2.virtual_ioc_fill = bank_capped_fill
    runner_error: BaseException | None = None
    try:
        await v2.run(args)
    except base.TargetClosedTradesReached:
        console.print(f"[green]Reached exact closed-trade target: {target}[/green]")
    except BankDepleted as exc:
        runner_error = exc
        console.print(f"[red]BANK DEPLETED after {len(closed_trades)} trades:[/red] {exc}")
    except BaseException as exc:
        runner_error = exc
        console.print(
            f"[red]Main runner stopped after {len(closed_trades)} closed trades:[/red] "
            f"{type(exc).__name__}: {exc}"
        )
    finally:
        v2._close_trade = original_close
        v2.delayed_catchup_entry_ok = original_delayed
        v2.virtual_ioc_fill = original_fill
        fair_stop.set()
        try:
            await asyncio.wait_for(fair_task, timeout=3.0)
        except (TimeoutError, asyncio.CancelledError):
            fair_task.cancel()

    if closed_trades:
        base._build_liquidation_report(
            closed_trades,
            fair_rows,
            risks,
            starting_balance_usdt=args.balance_usdt,
            margin_fraction=args.margin_fraction,
            requested_leverage=args.leverage,
            liquidation_fee_rate=args.liquidation_fee_rate,
            report_path=report_csv,
        )
    else:
        console.print("[red]No closed trades captured.[/red]")

    if runner_error is not None:
        console.print("[yellow]Fair-price trace and partial liquidation results were preserved despite the error.[/yellow]")


def main() -> None:
    args = base.build_parser().parse_args()
    if args.balance_usdt <= 0:
        raise SystemExit("--balance-usdt must be > 0")
    if not 0 < args.margin_fraction <= 1:
        raise SystemExit("--margin-fraction must be in (0, 1]")
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
