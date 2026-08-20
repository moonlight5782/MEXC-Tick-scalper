from __future__ import annotations

import asyncio
import math
import time
import uuid

from . import auto_discovery_testnet_xrp_fixed as fixed
from .execution import OrderFill, OrderSide, PositionSnapshot
from .web_execution import MexcWebError, MexcWebExecutionAdapter, WebExecutionConfig
from .web_fee import read_web_fee_status

# This wrapper is deliberately narrow:
#   * entry thresholds/gates/sizing/IOC remain 100% in xrp_fixed + baseline_v1;
#   * first positive executable Demo PnL arms winner hold;
#   * lead-lag thesis exits stop cutting a winner;
#   * hard mid-adverse safety remains active;
#   * no synthetic latency and no fixed software sleeps are added to execution;
#   * after a confirmed fill, position management starts immediately from that fill
#     instead of waiting for a separate private-position endpoint to catch up.

_ORIGINAL_TRAILING = fixed.PositiveTrailing
_ORIGINAL_WAIT_FOR_ORDER_RESULT = MexcWebExecutionAdapter._wait_for_order_result
_ORIGINAL_RESOLVE_REMOTE_POSITION = fixed._resolve_remote_position
_ORIGINAL_CLOSE_POSITION_FULLY = fixed._close_position_fully
_ORIGINAL_ACCOUNT_CLOSE = fixed._close

_ACTIVE_ARGS = None
_SAVED_PRE_PROFIT_POLICY: dict[str, float | int] = {}
_ORIGINAL_ARM_BPS: float | None = None
PROFIT_LOCK_FLOOR_BPS = 0.10
_LIVE_FEE_EST_TOTAL_USDT = 0.0
_LIVE_NET_EST_TOTAL_USDT = 0.0


def _capture_pre_profit_policy() -> None:
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
    if _ACTIVE_ARGS is None:
        return
    _capture_pre_profit_policy()
    # Keep hard adverse safety unchanged. Suppress only ordinary thesis/lifecycle
    # exits once the actual Demo position has positive executable PnL.
    _ACTIVE_ARGS.leader_retrace_exit_bps = math.inf
    _ACTIVE_ARGS.reversal_edge_bps = math.inf
    _ACTIVE_ARGS.no_progress_ms = 2_147_483_647
    _ACTIVE_ARGS.max_hold_ms = 2_147_483_647


class ProfitHoldTrailing:
    def __init__(self, distance_bps: float) -> None:
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
        stop = self._inner.update(move_bps)
        if move_bps > 0.0 and not self._armed:
            self._armed = True
            _arm_profit_hold()
            floor = min(PROFIT_LOCK_FLOOR_BPS, move_bps * 0.5)
            if floor > 0.0:
                self._inner.stop_bps = floor if self._inner.stop_bps is None else max(self._inner.stop_bps, floor)
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
    """Poll order state with no fixed software delay between HTTP responses."""
    deadline = time.monotonic() + timeout_seconds
    last: dict | None = None
    while time.monotonic() < deadline:
        try:
            last = await self._get_order_by_external_id(symbol, client_order_id)
        except MexcWebError:
            last = None
        if last and int(last.get("state") or 0) in (3, 4, 5):
            return last
    if last is not None:
        return last
    raise MexcWebError(f"order {client_order_id} was not observable after submit")


async def _immediate_position_from_fill(
    adapter: MexcWebExecutionAdapter,
    symbol: str,
    side: OrderSide,
    fill: OrderFill,
    leverage: int,
) -> PositionSnapshot:
    """Start position management immediately after confirmed fill.

    No get_positions() call is made here. The fill is already the exchange-confirmed
    execution result, so waiting for a second private endpoint only adds delay.
    """
    del adapter
    if fill.filled_qty <= 0:
        raise MexcWebError("IOC returned no fill")
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


async def _submit_exact_close(
    adapter: MexcWebExecutionAdapter,
    position: PositionSnapshot,
) -> OrderFill:
    client_id = f"tn-exit-{uuid.uuid4().hex}"[:32]
    if position.position_id:
        return await adapter.close_position_snapshot_reduce_only(position, client_order_id=client_id)
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
    """Preserve residual-close verification without fixed polling sleeps."""
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

    residual = await _find_same_position(adapter, current)
    if residual is not None:
        raise MexcWebError(
            f"Demo reduce-only close left residual position {residual.symbol} "
            f"positionId={residual.position_id} qty={residual.qty:g}"
        )
    if last_fill is None:
        raise MexcWebError("Demo close did not submit")
    return last_fill


async def _load_selected_live_fee(args) -> None:
    """Read selected-symbol LIVE fee once before the trading feeds start."""
    if hasattr(args, "selected_live_taker_fee_rate"):
        return
    cfg = WebExecutionConfig.from_env(write_enabled=False)
    async with MexcWebExecutionAdapter(cfg) as adapter:
        status = await read_web_fee_status(adapter, fixed.SYMBOL)
    args.selected_live_maker_fee_rate = status.maker
    args.selected_live_taker_fee_rate = status.taker
    maker = "?" if status.maker is None else f"{status.maker * 10_000.0:.2f}bps"
    taker = "?" if status.taker is None else f"{status.taker * 10_000.0:.2f}bps"
    fixed.console.print(
        f"[cyan]SELECTED LIVE FEE[/cyan] {fixed.SYMBOL} maker={maker} taker={taker}; "
        "fee is reporting-only and never blocks this selected pair from trading"
    )


async def _close_with_live_fee_reporting(adapter, pos, stats, output, reason) -> None:
    """Report LIVE fee economics after close without affecting any trade decision."""
    global _LIVE_FEE_EST_TOTAL_USDT, _LIVE_NET_EST_TOTAL_USDT

    gross_before = float(stats.gross_pnl_usdt)
    await _ORIGINAL_ACCOUNT_CLOSE(adapter, pos, stats, output, reason)
    gross = float(stats.gross_pnl_usdt) - gross_before

    maker = getattr(_ACTIVE_ARGS, "selected_live_maker_fee_rate", None) if _ACTIVE_ARGS is not None else None
    taker = getattr(_ACTIVE_ARGS, "selected_live_taker_fee_rate", None) if _ACTIVE_ARGS is not None else None
    if taker is None:
        fixed.console.print(
            f"[yellow]LIVE FEE[/yellow] {fixed.SYMBOL} maker={'?' if maker is None else maker} taker=?; "
            f"GROSS=${gross:+.4f}; LIVE_NET_EST=unknown (fee rate unavailable)"
        )
        return

    qty = max(0.0, float(pos.remote.qty))
    direction = float(pos.signal.direction)
    exit_price = float(pos.entry_price)
    if qty > 0 and direction != 0:
        exit_price = float(pos.entry_price) + gross / (direction * qty)
    entry_notional = abs(float(pos.entry_price) * qty)
    exit_notional = abs(exit_price * qty)
    live_fee = (entry_notional + exit_notional) * max(0.0, float(taker))
    live_net = gross - live_fee
    fee_bps = live_fee / max(float(pos.filled_notional), 1e-12) * 10_000.0

    _LIVE_FEE_EST_TOTAL_USDT += live_fee
    _LIVE_NET_EST_TOTAL_USDT += live_net
    maker_text = "?" if maker is None else f"{float(maker) * 10_000.0:.2f}bps"
    taker_text = f"{float(taker) * 10_000.0:.2f}bps"
    fixed.console.print(
        f"[bold cyan]LIVE FEE ECONOMICS[/bold cyan] {fixed.SYMBOL} "
        f"maker={maker_text} taker={taker_text} (IOC/market estimate uses taker both sides) "
        f"LIVE_FEE_EST=${live_fee:.4f} ({fee_bps:.2f}bps round-trip) "
        f"GROSS=${gross:+.4f} LIVE_NET_EST=${live_net:+.4f} "
        f"CUM_LIVE_FEES=${_LIVE_FEE_EST_TOTAL_USDT:.4f} CUM_LIVE_NET_EST=${_LIVE_NET_EST_TOTAL_USDT:+.4f}"
    )


async def run(args) -> None:
    global _ACTIVE_ARGS, _SAVED_PRE_PROFIT_POLICY, _ORIGINAL_ARM_BPS
    global _LIVE_FEE_EST_TOTAL_USDT, _LIVE_NET_EST_TOTAL_USDT

    # Read fee metadata before fixed.run starts its market feeds/trading loop.
    await _load_selected_live_fee(args)

    # Do not touch entry parameters. baseline_v1 remains exactly 8 bps / 3x.
    _ACTIVE_ARGS = args
    _SAVED_PRE_PROFIT_POLICY = {}
    _ORIGINAL_ARM_BPS = float(args.profit_runner_arm_bps)
    _LIVE_FEE_EST_TOTAL_USDT = 0.0
    _LIVE_NET_EST_TOTAL_USDT = 0.0

    # Exit-only change: arm winner mode on the first positive executable tick.
    args.profit_runner_arm_bps = 1e-9

    original_trailing = fixed.PositiveTrailing
    original_resolve = fixed._resolve_remote_position
    original_close = fixed._close_position_fully
    original_account_close = fixed._close
    original_wait = MexcWebExecutionAdapter._wait_for_order_result

    fixed.PositiveTrailing = ProfitHoldTrailing
    fixed._resolve_remote_position = _immediate_position_from_fill
    fixed._close_position_fully = _network_only_close_position_fully
    fixed._close = _close_with_live_fee_reporting
    MexcWebExecutionAdapter._wait_for_order_result = _network_only_wait_for_order_result

    try:
        fixed.console.print(
            "[bold cyan]XRP TESTNET VERIFIED MODE[/bold cyan] "
            "entry=baseline 8bps/3x unchanged; only network/MEXC waits remain; "
            "confirmed fill -> immediate position management; first positive PnL -> profit hold"
        )
        await fixed.run(args)
    finally:
        _restore_pre_profit_policy()
        if _ORIGINAL_ARM_BPS is not None:
            args.profit_runner_arm_bps = _ORIGINAL_ARM_BPS
        fixed.PositiveTrailing = original_trailing
        fixed._resolve_remote_position = original_resolve
        fixed._close_position_fully = original_close
        fixed._close = original_account_close
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
        fixed.console.print(f"[red]XRP VERIFIED TESTNET STOPPED:[/red] {type(exc).__name__}: {exc}")
        raise SystemExit(2)


if __name__ == "__main__":
    main()
