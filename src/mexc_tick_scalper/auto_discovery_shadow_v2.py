from __future__ import annotations

import asyncio
import time

from . import auto_discovery_shadow as base
from .liquidation_guard import (
    LiquidationState,
    MexcFairPriceFeed,
    build_isolated_liquidation_state,
    fair_distance_to_liquidation_bps,
    fair_price_crossed_liquidation,
)


_ACTIVE_ARGS = None
_ACTIVE_LIQ: LiquidationState | None = None
_FAIR_FEED: MexcFairPriceFeed | None = None
_ORIGINAL_AUTO_FILL = base._auto_sized_virtual_ioc_fill
_ORIGINAL_DISCOVER = base.discover
_ORIGINAL_EXIT_DEPTH = base.runner._exit_depth_for_qty
_ORIGINAL_AUTO_RECORD_CLOSE = base._auto_record_close


async def _discover_with_fair_feed(args):
    global _FAIR_FEED
    candidates = await _ORIGINAL_DISCOVER(args)
    if _FAIR_FEED is not None:
        await _FAIR_FEED.close()
    _FAIR_FEED = MexcFairPriceFeed([row.contract.mexc_symbol for row in candidates])
    await _FAIR_FEED.start()
    base.console.print(
        "[bold cyan]LIQUIDATION MODEL[/bold cyan] LIVE MEXC fairPrice + public contract MMR/risk tiers; "
        "isolated liquidation fee is not guessed, so liq price is explicitly an estimate."
    )
    return candidates


def _tracked_auto_fill(
    book, *, direction: int, target_notional_usdt: float,
    contract_size: float, cross_bps: float,
):
    """Synchronize dynamic IOC reporting and arm liquidation tracking for its actual fill."""
    global _ACTIVE_LIQ
    del target_notional_usdt
    symbol = base.CURRENT_SYMBOL
    requested, _, _ = base._requested_notional_and_margin(symbol)
    if _ACTIVE_ARGS is not None:
        _ACTIVE_ARGS.target_notional_usdt = requested
    fill = _ORIGINAL_AUTO_FILL(
        book,
        direction=direction,
        target_notional_usdt=requested,
        contract_size=contract_size,
        cross_bps=cross_bps,
    )
    if fill.qty > 0 and fill.avg_price > 0 and symbol in base.CONTRACTS:
        leverage = base._effective_leverage(symbol)
        state = build_isolated_liquidation_state(
            contract=base.CONTRACTS[symbol],
            symbol=symbol,
            direction=direction,
            entry_price=fill.avg_price,
            qty_base=fill.qty,
            leverage=leverage,
        )
        _ACTIVE_LIQ = state
        if state is not None:
            base.console.print(
                f"[cyan]LIQ RISK[/cyan] {symbol} {'LONG' if direction > 0 else 'SHORT'} "
                f"lev={state.leverage:.0f}x tier={state.risk_level} mmr={state.maintenance_margin_rate:.4%} "
                f"margin=${state.position_margin_usdt:.2f} maint=${state.maintenance_margin_usdt:.2f} "
                f"liq_est={state.liquidation_price:.10g} distance={state.liquidation_distance_bps:.1f}bps"
            )
    else:
        _ACTIVE_LIQ = None
    return fill


def _liquidation_aware_exit_depth(book, *, direction: int, qty: float, contract_size: float):
    """Model MEXC fair-price liquidation before any later shadow exit can fill."""
    state = _ACTIVE_LIQ
    feed = _FAIR_FEED
    if state is not None and feed is not None and state.direction == direction and qty > 0:
        now_ms = int(time.time() * 1000)
        fair = feed.fresh(state.symbol, now_ms)
        if fair is not None:
            remaining = fair_distance_to_liquidation_bps(state, fair.price)
            if fair_price_crossed_liquidation(state, fair.price):
                if not state.liquidated:
                    state.liquidated = True
                    state.liquidation_fair_price = fair.price
                    state.liquidation_seen_ms = now_ms
                    base.console.print(
                        f"[bold red]MEXC FAIR-PRICE LIQUIDATION[/bold red] {state.symbol} "
                        f"fair={fair.price:.10g} liq_est={state.liquidation_price:.10g} "
                        f"entry={state.entry_price:.10g} lev={state.leverage:.0f}x tier={state.risk_level}"
                    )
                # Forced liquidation is not a normal order-book fill.  Use the observed
                # fair price at/after crossing instead of pretending our IOC exit filled.
                return float(qty), float(fair.price)
            if remaining <= state.liquidation_distance_bps * 0.25:
                base.console.print(
                    f"[bold yellow]LIQ BUFFER DANGER[/bold yellow] {state.symbol} "
                    f"fair={fair.price:.10g} remaining={remaining:.1f}bps "
                    f"of entry_buffer={state.liquidation_distance_bps:.1f}bps; emergency exit policy remains immediate."
                )
    return _ORIGINAL_EXIT_DEPTH(
        book,
        direction=direction,
        qty=qty,
        contract_size=contract_size,
    )


def _liquidation_aware_record_close(stats, row) -> None:
    global _ACTIVE_LIQ
    state = _ACTIVE_LIQ
    if state is not None and state.symbol == row.symbol and state.liquidated:
        row.exit_reason = "mexc_fair_price_liquidation"
        base.console.print(
            f"[bold red]LIQUIDATION ACCOUNTING[/bold red] {row.symbol} "
            f"fair_cross={state.liquidation_fair_price} liq_est={state.liquidation_price:.10g} "
            f"margin_at_risk=${state.position_margin_usdt:.2f}"
        )
    _ORIGINAL_AUTO_RECORD_CLOSE(stats, row)
    if state is not None and state.symbol == row.symbol:
        _ACTIVE_LIQ = None


async def run(args):
    global _ACTIVE_ARGS, _ACTIVE_LIQ, _FAIR_FEED
    _ACTIVE_ARGS = args
    _ACTIVE_LIQ = None
    original_fill = base._auto_sized_virtual_ioc_fill
    original_discover = base.discover
    original_exit_depth = base.runner._exit_depth_for_qty
    original_record_close = base._auto_record_close
    base._auto_sized_virtual_ioc_fill = _tracked_auto_fill
    base.discover = _discover_with_fair_feed
    base.runner._exit_depth_for_qty = _liquidation_aware_exit_depth
    base._auto_record_close = _liquidation_aware_record_close
    try:
        return await base.run(args)
    finally:
        base._auto_sized_virtual_ioc_fill = original_fill
        base.discover = original_discover
        base.runner._exit_depth_for_qty = original_exit_depth
        base._auto_record_close = original_record_close
        if _FAIR_FEED is not None:
            await _FAIR_FEED.close()
        _FAIR_FEED = None
        _ACTIVE_LIQ = None
        _ACTIVE_ARGS = None


def main() -> None:
    args = base.build_parser().parse_args()
    base.apply_baseline_v1(args)
    if args.discovery_top <= 0:
        raise SystemExit("--discovery-top must be positive")
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
