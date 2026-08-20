from __future__ import annotations

import asyncio
import time
from typing import Any

from . import auto_discovery_testnet_xrp_fixed as fixed
from .execution import OrderFill, OrderSide, PositionSnapshot
from .web_execution import MexcWebExecutionAdapter

# Temporary XRP Testnet plumbing policy only. The production/shadow strategy is untouched.
TESTNET_MIN_RESIDUAL_BPS = 15.0
TESTNET_MIN_STRENGTH_RATIO = 4.0
DEMO_PRICE_CACHE_MAX_AGE_MS = 100.0
DEMO_PRICE_REFRESH_SECONDS = 0.075

_ORIGINAL_PROBE = MexcWebExecutionAdapter.probe
_ORIGINAL_GET_BEST = MexcWebExecutionAdapter.get_best_price
_ORIGINAL_CLOSE = MexcWebExecutionAdapter.close
_ORIGINAL_OPEN_IOC = MexcWebExecutionAdapter.open_ioc


def _cache(adapter: MexcWebExecutionAdapter) -> dict[OrderSide, tuple[float, float]]:
    cache = getattr(adapter, "_xrp_demo_best_cache", None)
    if cache is None:
        cache = {}
        setattr(adapter, "_xrp_demo_best_cache", cache)
    return cache


async def _refresh_demo_best(adapter: MexcWebExecutionAdapter) -> None:
    while True:
        try:
            # Fetch both sides concurrently so the cache is already warm before a signal arrives.
            long_task = asyncio.create_task(_ORIGINAL_GET_BEST(adapter, fixed.SYMBOL, OrderSide.LONG))
            short_task = asyncio.create_task(_ORIGINAL_GET_BEST(adapter, fixed.SYMBOL, OrderSide.SHORT))
            long_best, short_best = await asyncio.gather(long_task, short_task)
            now_ms = time.time_ns() / 1_000_000.0
            rows = _cache(adapter)
            rows[OrderSide.LONG] = (float(long_best), now_ms)
            rows[OrderSide.SHORT] = (float(short_best), now_ms)
        except asyncio.CancelledError:
            raise
        except Exception:
            # A transient public Demo depth failure must not kill the trading loop.
            pass
        await asyncio.sleep(DEMO_PRICE_REFRESH_SECONDS)


async def _fast_probe(self: MexcWebExecutionAdapter) -> dict[str, Any]:
    result = await _ORIGINAL_PROBE(self)
    task = getattr(self, "_xrp_demo_best_task", None)
    if task is None or task.done():
        setattr(self, "_xrp_demo_best_task", asyncio.create_task(_refresh_demo_best(self)))
    # Warm the cache once during startup, outside the signal critical path.
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        rows = _cache(self)
        if OrderSide.LONG in rows and OrderSide.SHORT in rows:
            break
        await asyncio.sleep(0.01)
    return result


async def _cached_get_best(self: MexcWebExecutionAdapter, symbol: str, side: OrderSide) -> float:
    if symbol.upper() != fixed.SYMBOL:
        return await _ORIGINAL_GET_BEST(self, symbol, side)
    row = _cache(self).get(side)
    now_ms = time.time_ns() / 1_000_000.0
    if row is not None:
        price, recv_ms = row
        if now_ms - recv_ms <= DEMO_PRICE_CACHE_MAX_AGE_MS and price > 0:
            return price
    # Safety fallback: never use a stale price merely to save latency.
    price = await _ORIGINAL_GET_BEST(self, symbol, side)
    _cache(self)[side] = (float(price), time.time_ns() / 1_000_000.0)
    return float(price)


async def _fast_close_adapter(self: MexcWebExecutionAdapter) -> None:
    task = getattr(self, "_xrp_demo_best_task", None)
    if task is not None:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        setattr(self, "_xrp_demo_best_task", None)
    await _ORIGINAL_CLOSE(self)


async def _timed_open_ioc(
    self: MexcWebExecutionAdapter,
    *,
    symbol: str,
    side: OrderSide,
    price: float,
    qty: float,
    leverage: int,
    client_order_id: str,
    timing_marks: dict[str, float] | None = None,
) -> OrderFill:
    marks: dict[str, float] = timing_marks if timing_marks is not None else {}
    started = time.time_ns() / 1_000_000.0
    fill = await _ORIGINAL_OPEN_IOC(
        self,
        symbol=symbol,
        side=side,
        price=price,
        qty=qty,
        leverage=leverage,
        client_order_id=client_order_id,
        timing_marks=marks,
    )
    done = time.time_ns() / 1_000_000.0
    post_start = marks.get("ioc_post_start_ms", started)
    post_response = marks.get("ioc_post_response_ms", done)
    confirmed = marks.get("ioc_confirmed_ms", done)
    fixed.console.print(
        f"TESTNET ENTRY LATENCY submit_prep={post_start-started:.1f}ms "
        f"POST={post_response-post_start:.1f}ms confirm_after_POST={confirmed-post_response:.1f}ms "
        f"open_ioc_total={done-started:.1f}ms"
    )
    return fill


async def _provisional_position(
    adapter: MexcWebExecutionAdapter,
    symbol: str,
    side: OrderSide,
    fill: OrderFill,
    leverage: int,
) -> PositionSnapshot:
    del adapter
    if fill.filled_qty <= 0:
        raise RuntimeError("IOC returned no fill")
    # open_ioc already waited until the order result exposed dealVol/dealAvgPrice/positionId.
    # Do not add another private get_positions RTT before recording the entry timestamp.
    return PositionSnapshot(
        symbol=symbol,
        side=side,
        qty=fill.filled_qty,
        entry_price=fill.avg_price,
        leverage=leverage,
        isolated=True,
        position_id=fill.position_id,
        liquidation_price=None,
    )


async def run(args) -> None:
    # Temporary stronger-signal filter for the forced-XRP Testnet plumbing run only.
    args.min_absolute_residual_bps = max(float(args.min_absolute_residual_bps), TESTNET_MIN_RESIDUAL_BPS)
    args.min_signal_strength_ratio = max(float(args.min_signal_strength_ratio), TESTNET_MIN_STRENGTH_RATIO)

    original_resolve = fixed._resolve_remote_position
    fixed._resolve_remote_position = _provisional_position
    MexcWebExecutionAdapter.probe = _fast_probe
    MexcWebExecutionAdapter.get_best_price = _cached_get_best
    MexcWebExecutionAdapter.open_ioc = _timed_open_ioc
    MexcWebExecutionAdapter.close = _fast_close_adapter
    try:
        fixed.console.print(
            f"[bold cyan]FAST XRP TESTNET[/bold cyan] residual>={args.min_absolute_residual_bps:.1f}bps "
            f"strength>={args.min_signal_strength_ratio:.1f}x; Demo best-price cache<={DEMO_PRICE_CACHE_MAX_AGE_MS:.0f}ms"
        )
        fixed.console.print(
            "Critical path: cached Demo best -> IOC POST -> order confirmation. "
            "The extra get_positions RTT after fill is removed."
        )
        await fixed.run(args)
    finally:
        fixed._resolve_remote_position = original_resolve
        MexcWebExecutionAdapter.probe = _ORIGINAL_PROBE
        MexcWebExecutionAdapter.get_best_price = _ORIGINAL_GET_BEST
        MexcWebExecutionAdapter.open_ioc = _ORIGINAL_OPEN_IOC
        MexcWebExecutionAdapter.close = _ORIGINAL_CLOSE


def main() -> None:
    args = fixed.build_parser().parse_args()
    fixed.auto.apply_baseline_v1(args)
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        raise SystemExit(130)
    except Exception as exc:
        fixed.console.print(f"[red]FAST XRP TESTNET STOPPED:[/red] {type(exc).__name__}: {exc}")
        raise SystemExit(2)


if __name__ == "__main__":
    main()
