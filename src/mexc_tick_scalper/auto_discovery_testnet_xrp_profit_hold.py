from __future__ import annotations

import asyncio
import math
import time
import uuid

from . import auto_discovery_testnet_xrp_fixed as fixed
from .execution import OrderFill, OrderSide, PositionSnapshot
from .web_execution import MexcWebError, MexcWebExecutionAdapter

# This wrapper is deliberately narrow:
#   * entry thresholds/gates/sizing/IOC remain 100% in xrp_fixed + baseline_v1;
#   * first positive executable Demo PnL arms winner hold;
#   * lead-lag thesis exits stop cutting a winner;
#   * hard mid-adverse safety remains active;
#   * fixed software sleeps are removed from order/position polling so observed
#     latency is dominated by actual HTTP/network/MEXC response time.

_ORIGINAL_TRAILING = fixed.PositiveTrailing
_ORIGINAL_WAIT_FOR_ORDER_RESULT = MexcWebExecutionAdapter._wait_for_order_result
_ORIGINAL_RESOLVE_REMOTE_POSITION = fixed._resolve_remote_position
_ORIGINAL_CLOSE_POSITION_FULLY = fixed._close_position_fully

_ACTIVE_ARGS = None
_SAVED_PRE_PROFIT_POLICY: dict[str, float | int] = {}
_ORIGINAL_ARM_BPS: float | None = None
PROFIT_LOCK_FLOOR_BPS = 0.10


def _capture_pre_profit_policy() -> None:
    """Capture policy after xrp_fixed applies its normal immediate protections."""
    global _SAVED_PRE_PROFIT_POLICY
    if _ACTIVE_ARGS is None or _SAVED_PRE_PROFIT_POLICY:
        return
    _SAVED_PRE_PROFIT_POLICY = {
        "mid_adverse_cut_bps": _ACTIVE_ARGS.mid_adverse_cut_bps,
        "leader_retrace_exit_bps": _ACTIVE_ARGS.leader_retrace_exit_bps,
        "reversal_edge_bps": _ACTIVE_ARGS.reversal_edge_bps,
        "no_progress_ms": _ACTIVE_ARGS.no_progress_ms,
        "max_hold_ms": _ACTIVE_ARGS.max_hold_ms,
    }


def _restore_pre_profit_policy() -> None:
    if _ACTIVE_ARGS is None or not _SAVED_PRE_PROFIT_POLICY:
        return
    _ACTIVE_ARGS.mid_adverse_cut_bps = _SAVED_PRE_PROFIT_POLICY["mid_adverse_cut_bps"]
    _ACTIVE_ARGS.leader_retrace_exit_bps = _SAVED_PRE_PROFIT_POLICY["leader_retrace_exit_bps"]
    _ACTIVE_ARGS.reversal_edge_bps = _SAVED_PRE_PROFIT_POLICY["reversal_edge_bps"]
    _ACTIVE_ARGS.no_progress_ms = _SAVED_PRE_PROFIT_POLICY["no_progress_ms"]
    _ACTIVE_ARGS.max_hold_ms = _SAVED_PRE_PROFIT_POLICY["max_hold_ms"]


def _arm_profit_hold() -> None:
    """Make trailing own the winner while preserving hard adverse safety."""
    if _ACTIVE_ARGS is None:
        return
    _capture_pre_profit_policy()

    # Keep mid_adverse_cut_bps unchanged: this is the emergency/hard protection.
    # Suppress only lead-lag thesis/lifecycle exits once actual Demo PnL is positive.
    _ACTIVE_ARGS.leader_retrace_exit_bps = math.inf
    _ACTIVE_ARGS.reversal_edge_bps = math.inf
    _ACTIVE_ARGS.no_progress_ms = 2_147_483_647
    _ACTIVE_ARGS.max_hold_ms = 2_147_483_647


class ProfitHoldTrailing:
    """Original trailing plus permanent winner-hold after first positive PnL."""

    def __init__(self, distance_bps: float) -> None:
        # New position starts with exactly the original pre-profit protections.
        _restore_pre_profit_policy()
        self._inner = _ORIGINAL_TRAILING(distance_bps=distance_bps)
        self._armed = False

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
        # Preserve the original +3/+5/+6 trailing ladder exactly.
        stop = self._inner.update(move_bps)

        if move_bps > 0.0 and not self._armed:
            self._armed = True
            _arm_profit_hold()

            # Immediately lock a strictly positive gross floor, but never put the
            # decision threshold above currently executable profit.
            floor = min(PROFIT_LOCK_FLOOR_BPS, move_bps * 0.5)
            if floor > 0.0:
                self._inner.stop_bps = (
                    floor if self._inner.stop_bps is None
                    else max(self._inner.stop_bps, floor)
                )

            fixed.console.print(
                f"[bold green]PROFIT HOLD ARMED[/bold green] {fixed.SYMBOL} "
                f"executable={move_bps:+.2f}bps stop={self._inner.stop_bps:+.2f}bps; "
                "trailing owns winner; convergence/retrace/reversal/no-progress/timeout suppressed; "
                "hard mid-adverse safety remains"
            )

        return self._inner.stop_bps if self._armed else stop


async def _network_only_wait_for_order_result(
    self: MexcWebExecutionAdapter,
    symbol: str,
    client_order_id: str,
    timeout_seconds: float = 1.2,
) -> dict:
    """Poll immediately after each HTTP response; add no fixed sleep between polls."""
    deadline = time.monotonic() + timeout_seconds
    last: dict | None = None
    while time.monotonic() < deadline:
        try:
            last = await self._get_order_by_external_id(symbol, client_order_id)
        except MexcWebError:
            last = None
        if last and int(last.get("state") or 0) in (3, 4, 5):
            return last
        # No sleep here: the network request itself determines the polling cadence.
    if last is not None:
        return last
    raise MexcWebError(f"order {client_order_id} was not observable after submit")


async def _network_only_resolve_remote_position(
    adapter: MexcWebExecutionAdapter,
    symbol: str,
    side: OrderSide,
    fill: OrderFill,
    leverage: int,
) -> PositionSnapshot:
    """Resolve the actual Demo position without adding a fixed 25 ms poll delay."""
    if fill.filled_qty <= 0:
        raise MexcWebError("IOC returned no fill")
    provisional = PositionSnapshot(
        symbol=symbol,
        side=side,
        qty=fill.filled_qty,
        entry_price=fill.avg_price,
        leverage=leverage,
        isolated=True,
        position_id=fill.position_id,
    )
    deadline = time.monotonic() + 0.75
    while time.monotonic() < deadline:
        rows = await adapter.get_positions(symbol)
        matching = next((row for row in rows if row.side is side), None)
        if matching is not None:
            return matching
        # No software sleep: next poll starts as soon as previous HTTP call returns.
    return provisional


async def _submit_exact_close(
    adapter: MexcWebExecutionAdapter,
    position: PositionSnapshot,
) -> OrderFill:
    client_id = f"tn-exit-{uuid.uuid4().hex}"[:32]
    if position.position_id:
        return await adapter.close_position_snapshot_reduce_only(
            position,
            client_order_id=client_id,
        )
    return await adapter.close_market_reduce_only(
        symbol=position.symbol,
        qty=position.qty,
        side=OrderSide.SHORT if position.side is OrderSide.LONG else OrderSide.LONG,
        client_order_id=client_id,
    )


async def _find_same_position(
    adapter: MexcWebExecutionAdapter,
    position: PositionSnapshot,
) -> PositionSnapshot | None:
    rows = await adapter.get_positions(position.symbol)
    if position.position_id is not None:
        return next((row for row in rows if row.position_id == position.position_id), None)
    return next((row for row in rows if row.side is position.side), None)


async def _network_only_close_position_fully(
    adapter: MexcWebExecutionAdapter,
    position: PositionSnapshot,
    *,
    attempts: int = 4,
) -> OrderFill:
    """Preserve residual-close protection but remove fixed 40 ms poll sleeps."""
    current = position
    last_fill: OrderFill | None = None
    for _ in range(max(1, attempts)):
        last_fill = await _submit_exact_close(adapter, current)
        deadline = time.monotonic() + 0.75
        while time.monotonic() < deadline:
            residual = await _find_same_position(adapter, current)
            if residual is None:
                return last_fill
            current = residual
            # No software sleep: HTTP/MEXC response time is the only poll spacing.

    residual = await _find_same_position(adapter, current)
    if residual is not None:
        raise MexcWebError(
            f"Demo reduce-only close left residual position {residual.symbol} "
            f"positionId={residual.position_id} qty={residual.qty:g}"
        )
    if last_fill is None:
        raise MexcWebError("Demo close did not submit")
    return last_fill


async def run(args) -> None:
    global _ACTIVE_ARGS, _SAVED_PRE_PROFIT_POLICY, _ORIGINAL_ARM_BPS

    # IMPORTANT: no entry parameter is changed here. baseline_v1 remains 8 bps / 3x.
    _ACTIVE_ARGS = args
    _SAVED_PRE_PROFIT_POLICY = {}
    _ORIGINAL_ARM_BPS = float(args.profit_runner_arm_bps)

    # xrp_fixed uses runner_armed only to disable convergence. Make that flag arm on
    # the same first-positive tick as ProfitHoldTrailing. This affects exits only.
    args.profit_runner_arm_bps = 1e-9

    original_trailing = fixed.PositiveTrailing
    original_resolve = fixed._resolve_remote_position
    original_close = fixed._close_position_fully
    original_wait = MexcWebExecutionAdapter._wait_for_order_result

    fixed.PositiveTrailing = ProfitHoldTrailing
    fixed._resolve_remote_position = _network_only_resolve_remote_position
    fixed._close_position_fully = _network_only_close_position_fully
    MexcWebExecutionAdapter._wait_for_order_result = _network_only_wait_for_order_result

    try:
        fixed.console.print(
            "[bold cyan]XRP TESTNET VERIFIED MODE[/bold cyan] "
            "entry=baseline 8bps/3x unchanged; no fixed software polling delays; "
            "first positive executable PnL -> profit hold"
        )
        await fixed.run(args)
    finally:
        _restore_pre_profit_policy()
        if _ORIGINAL_ARM_BPS is not None:
            args.profit_runner_arm_bps = _ORIGINAL_ARM_BPS
        fixed.PositiveTrailing = original_trailing
        fixed._resolve_remote_position = original_resolve
        fixed._close_position_fully = original_close
        MexcWebExecutionAdapter._wait_for_order_result = original_wait
        _ACTIVE_ARGS = None
        _SAVED_PRE_PROFIT_POLICY = {}
        _ORIGINAL_ARM_BPS = None


def main() -> None:
    args = fixed.build_parser().parse_args()
    fixed.auto.apply_baseline_v1(args)
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        raise SystemExit(130)
    except Exception as exc:
        fixed.console.print(
            f"[red]XRP VERIFIED TESTNET STOPPED:[/red] {type(exc).__name__}: {exc}"
        )
        raise SystemExit(2)


if __name__ == "__main__":
    main()
