from __future__ import annotations

import asyncio
import math
import time
from typing import Any

from . import auto_discovery_testnet_xrp_fixed as fixed
from .execution import OrderFill, OrderSide, PositionSnapshot
from .web_execution import MexcWebExecutionAdapter

# Temporary XRP Testnet plumbing policy only. Production/shadow strategy is untouched.
# The first fast run showed 18-19.6 bps signals still dying before confirmed fill,
# so force a wider cross-exchange residual while we validate Demo execution latency.
TESTNET_MIN_RESIDUAL_BPS = 20.0
TESTNET_MIN_STRENGTH_RATIO = 4.0

# Demo depth HTTP itself is ~300 ms. Keep the latest already-fetched top for the IOC
# limit so no blocking depth GET is inserted between signal and order POST.
DEMO_PRICE_CACHE_SOFT_AGE_MS = 750.0
DEMO_PRICE_REFRESH_SECONDS = 0.025

_ORIGINAL_PROBE = MexcWebExecutionAdapter.probe
_ORIGINAL_GET_BEST = MexcWebExecutionAdapter.get_best_price
_ORIGINAL_CLOSE = MexcWebExecutionAdapter.close
_ORIGINAL_OPEN_IOC = MexcWebExecutionAdapter.open_ioc
_ORIGINAL_TRAILING = fixed.PositiveTrailing

_ACTIVE_ARGS = None
_EXIT_POLICY: dict[str, float | int] = {}


def _restore_normal_exit_policy() -> None:
    """Restore normal losing/flat-position exits before a new position is tracked."""
    if _ACTIVE_ARGS is None or not _EXIT_POLICY:
        return
    _ACTIVE_ARGS.mid_adverse_cut_bps = _EXIT_POLICY["mid_adverse_cut_bps"]
    _ACTIVE_ARGS.leader_retrace_exit_bps = _EXIT_POLICY["leader_retrace_exit_bps"]
    _ACTIVE_ARGS.reversal_edge_bps = _EXIT_POLICY["reversal_edge_bps"]
    _ACTIVE_ARGS.no_progress_ms = _EXIT_POLICY["no_progress_ms"]
    _ACTIVE_ARGS.max_hold_ms = _EXIT_POLICY["max_hold_ms"]


def _arm_profit_hold() -> None:
    """Once executable Demo PnL is positive, trailing owns the exit decision."""
    if _ACTIVE_ARGS is None:
        return
    _ACTIVE_ARGS.mid_adverse_cut_bps = math.inf
    _ACTIVE_ARGS.leader_retrace_exit_bps = math.inf
    _ACTIVE_ARGS.reversal_edge_bps = math.inf
    _ACTIVE_ARGS.no_progress_ms = 2_147_483_647
    _ACTIVE_ARGS.max_hold_ms = 2_147_483_647


class _ProfitHoldTrailing:
    """Arm on first positive executable PnL and never give the winner back to strategy exits.

    Before the position becomes profitable, the normal lead-lag exits remain active.
    On the first positive Demo executable PnL:
      * floor stop is immediately moved to breakeven (0 bps),
      * leader retrace / residual reversal / convergence / no-progress / timeout
        cease to close the winner,
      * the existing trailing ladder keeps ratcheting the stop upward:
        +3 bps peak -> +0.5 bps stop,
        +5 bps peak -> +2 bps stop,
        +6 bps and above -> peak minus trailing distance.
    """

    def __init__(self, distance_bps: float) -> None:
        # A new position starts with the normal exit policy until it first turns positive.
        _restore_normal_exit_policy()
        self._inner = _ORIGINAL_TRAILING(distance_bps=distance_bps)
        self._profit_hold_armed = False

    @property
    def distance_bps(self) -> float:
        return self._inner.distance_bps

    @property
    def peak_bps(self) -> float:
        return self._inner.peak_bps

    @property
    def stop_bps(self) -> float | None:
        return self._inner.stop_bps

    @stop_bps.setter
    def stop_bps(self, value: float | None) -> None:
        self._inner.stop_bps = value

    def update(self, move_bps: float) -> float | None:
        stop = self._inner.update(move_bps)
        if move_bps > 0.0 and not self._profit_hold_armed:
            self._profit_hold_armed = True
            _arm_profit_hold()
            self._inner.stop_bps = max(0.0, self._inner.stop_bps or 0.0)
            stop = self._inner.stop_bps
            fixed.console.print(
                f"[bold green]PROFIT HOLD ARMED[/bold green] {fixed.SYMBOL} "
                f"executable={move_bps:+.2f}bps stop_floor=0.00bps; "
                "leader/reversal/convergence/no-progress/timeout suppressed; trailing owns exit"
            )
        elif self._profit_hold_armed:
            # Never lower the floor below breakeven after the position has been profitable.
            self._inner.stop_bps = max(0.0, self._inner.stop_bps or 0.0)
            stop = self._inner.stop_bps
        return stop


def _cache(adapter: MexcWebExecutionAdapter) -> dict[OrderSide, tuple[float, float]]:
    cache = getattr(adapter, "_xrp_demo_best_cache", None)
    if cache is None:
        cache = {}
        setattr(adapter, "_xrp_demo_best_cache", cache)
    return cache


async def _refresh_demo_best(adapter: MexcWebExecutionAdapter) -> None:
    while True:
        try:
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
            pass
        await asyncio.sleep(DEMO_PRICE_REFRESH_SECONDS)


async def _fast_probe(self: MexcWebExecutionAdapter) -> dict[str, Any]:
    result = await _ORIGINAL_PROBE(self)
    task = getattr(self, "_xrp_demo_best_task", None)
    if task is None or task.done():
        setattr(self, "_xrp_demo_best_task", asyncio.create_task(_refresh_demo_best(self)))
    deadline = time.monotonic() + 3.0
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
    if row is None:
        return await _ORIGINAL_GET_BEST(self, symbol, side)
    price, recv_ms = row
    age_ms = time.time_ns() / 1_000_000.0 - recv_ms
    if age_ms > DEMO_PRICE_CACHE_SOFT_AGE_MS:
        fixed.console.print(
            f"[yellow]DEMO TOP CACHE AGE[/yellow] {age_ms:.0f}ms; using last LIMIT top without blocking HTTP"
        )
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
    global _ACTIVE_ARGS, _EXIT_POLICY

    args.min_absolute_residual_bps = max(float(args.min_absolute_residual_bps), TESTNET_MIN_RESIDUAL_BPS)
    args.min_signal_strength_ratio = max(float(args.min_signal_strength_ratio), TESTNET_MIN_STRENGTH_RATIO)

    # The previously agreed winner policy: first positive executable PnL arms the
    # runner immediately. The trailing ladder then owns the exit.
    args.profit_runner_arm_bps = min(float(args.profit_runner_arm_bps), 1e-9)
    _ACTIVE_ARGS = args
    _EXIT_POLICY = {
        "mid_adverse_cut_bps": args.mid_adverse_cut_bps,
        "leader_retrace_exit_bps": args.leader_retrace_exit_bps,
        "reversal_edge_bps": args.reversal_edge_bps,
        "no_progress_ms": args.no_progress_ms,
        "max_hold_ms": args.max_hold_ms,
    }

    original_resolve = fixed._resolve_remote_position
    original_trailing = fixed.PositiveTrailing
    fixed._resolve_remote_position = _provisional_position
    fixed.PositiveTrailing = _ProfitHoldTrailing
    MexcWebExecutionAdapter.probe = _fast_probe
    MexcWebExecutionAdapter.get_best_price = _cached_get_best
    MexcWebExecutionAdapter.open_ioc = _timed_open_ioc
    MexcWebExecutionAdapter.close = _fast_close_adapter
    try:
        fixed.console.print(
            f"[bold cyan]FAST XRP TESTNET[/bold cyan] residual>={args.min_absolute_residual_bps:.1f}bps "
            f"strength>={args.min_signal_strength_ratio:.1f}x; no blocking Demo-depth GET on signal path"
        )
        fixed.console.print(
            "Winner policy: first positive executable PnL -> breakeven floor -> trailing only; "
            "leader/reversal/convergence/no-progress/timeout cannot cut a profitable runner."
        )
        await fixed.run(args)
    finally:
        _restore_normal_exit_policy()
        _ACTIVE_ARGS = None
        _EXIT_POLICY = {}
        fixed._resolve_remote_position = original_resolve
        fixed.PositiveTrailing = original_trailing
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
