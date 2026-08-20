from __future__ import annotations

import asyncio
import math
import time
import uuid

from . import auto_discovery_testnet_xrp_fixed as fixed
from .execution import OrderFill, OrderSide, PositionSnapshot
from .web_execution import MexcWebError, MexcWebExecutionAdapter

PROFIT_LOCK_FLOOR_BPS = 0.10


class ProfitHoldTrailing:
    def __init__(self, runtime: "ProfitHoldRuntime", distance_bps: float) -> None:
        runtime.restore_pre_profit_policy()
        self.runtime = runtime
        self._inner = runtime.base_trailing(distance_bps=distance_bps)
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
            self.runtime.arm_profit_hold()
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


class ProfitHoldRuntime:
    """Scoped Testnet execution policy. All temporary overrides live here."""

    def __init__(self, args) -> None:
        self.args = args
        self.saved_policy: dict[str, float | int] = {}
        self.original_arm_bps = float(args.profit_runner_arm_bps)
        self.base_trailing = fixed.PositiveTrailing
        self.original_resolve = fixed._resolve_remote_position
        self.original_close = fixed._close_position_fully
        self.original_wait = MexcWebExecutionAdapter._wait_for_order_result

    def capture_pre_profit_policy(self) -> None:
        if self.saved_policy:
            return
        self.saved_policy = {
            "mid_adverse_cut_bps": self.args.mid_adverse_cut_bps,
            "leader_retrace_exit_bps": self.args.leader_retrace_exit_bps,
            "reversal_edge_bps": self.args.reversal_edge_bps,
            "no_progress_ms": self.args.no_progress_ms,
            "max_hold_ms": self.args.max_hold_ms,
        }

    def restore_pre_profit_policy(self) -> None:
        if not self.saved_policy:
            return
        self.args.mid_adverse_cut_bps = self.saved_policy["mid_adverse_cut_bps"]
        self.args.leader_retrace_exit_bps = self.saved_policy["leader_retrace_exit_bps"]
        self.args.reversal_edge_bps = self.saved_policy["reversal_edge_bps"]
        self.args.no_progress_ms = self.saved_policy["no_progress_ms"]
        self.args.max_hold_ms = self.saved_policy["max_hold_ms"]

    def arm_profit_hold(self) -> None:
        self.capture_pre_profit_policy()
        self.args.leader_retrace_exit_bps = math.inf
        self.args.reversal_edge_bps = math.inf
        self.args.no_progress_ms = 2_147_483_647
        self.args.max_hold_ms = 2_147_483_647

    def trailing_factory(self, distance_bps: float) -> ProfitHoldTrailing:
        return ProfitHoldTrailing(self, distance_bps)

    async def network_only_wait_for_order_result(
        self,
        adapter: MexcWebExecutionAdapter,
        symbol: str,
        client_order_id: str,
        timeout_seconds: float = 1.2,
    ) -> dict:
        deadline = time.monotonic() + timeout_seconds
        last: dict | None = None
        while time.monotonic() < deadline:
            try:
                last = await adapter._get_order_by_external_id(symbol, client_order_id)
            except MexcWebError:
                last = None
            if last and int(last.get("state") or 0) in (3, 4, 5):
                return last
        if last is not None:
            return last
        raise MexcWebError(f"order {client_order_id} was not observable after submit")

    async def immediate_position_from_fill(
        self,
        adapter: MexcWebExecutionAdapter,
        symbol: str,
        side: OrderSide,
        fill: OrderFill,
        leverage: int,
    ) -> PositionSnapshot:
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

    async def submit_exact_close(
        self,
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

    async def find_same_position(
        self,
        adapter: MexcWebExecutionAdapter,
        position: PositionSnapshot,
    ) -> PositionSnapshot | None:
        rows = await adapter.get_positions(position.symbol)
        if position.position_id is not None:
            return next((row for row in rows if row.position_id == position.position_id), None)
        return next((row for row in rows if row.side is position.side), None)

    async def close_position_fully(
        self,
        adapter: MexcWebExecutionAdapter,
        position: PositionSnapshot,
        *,
        attempts: int = 4,
    ) -> OrderFill:
        current = position
        last_fill: OrderFill | None = None
        for _ in range(max(1, attempts)):
            last_fill = await self.submit_exact_close(adapter, current)
            deadline = time.monotonic() + 0.75
            while time.monotonic() < deadline:
                residual = await self.find_same_position(adapter, current)
                if residual is None:
                    return last_fill
                current = residual

        residual = await self.find_same_position(adapter, current)
        if residual is not None:
            raise MexcWebError(
                f"Demo reduce-only close left residual position {residual.symbol} "
                f"positionId={residual.position_id} qty={residual.qty:g}"
            )
        if last_fill is None:
            raise MexcWebError("Demo close did not submit")
        return last_fill

    async def run(self) -> None:
        self.args.profit_runner_arm_bps = 1e-9

        async def wait_override(adapter, symbol, client_order_id, timeout_seconds=1.2):
            return await self.network_only_wait_for_order_result(
                adapter, symbol, client_order_id, timeout_seconds
            )

        fixed.PositiveTrailing = self.trailing_factory
        fixed._resolve_remote_position = self.immediate_position_from_fill
        fixed._close_position_fully = self.close_position_fully
        MexcWebExecutionAdapter._wait_for_order_result = wait_override
        try:
            fixed.console.print(
                "[bold cyan]TESTNET PROFIT-HOLD POLICY[/bold cyan] "
                "entry baseline unchanged; confirmed fill -> immediate management; "
                "first positive executable PnL -> profit hold"
            )
            await fixed.run(self.args)
        finally:
            self.restore_pre_profit_policy()
            self.args.profit_runner_arm_bps = self.original_arm_bps
            fixed.PositiveTrailing = self.base_trailing
            fixed._resolve_remote_position = self.original_resolve
            fixed._close_position_fully = self.original_close
            MexcWebExecutionAdapter._wait_for_order_result = self.original_wait


async def run(args) -> None:
    await ProfitHoldRuntime(args).run()


def main() -> None:
    args = fixed.build_parser().parse_args()
    fixed.auto.apply_baseline_v1(args)
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        raise SystemExit(130)
    except Exception as exc:
        fixed.console.print(f"[red]TESTNET PROFIT-HOLD STOPPED:[/red] {type(exc).__name__}: {exc}")
        raise SystemExit(2)


if __name__ == "__main__":
    main()
