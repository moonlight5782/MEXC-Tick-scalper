import asyncio
from types import SimpleNamespace
import math

import mexc_tick_scalper.auto_discovery_testnet_xrp_profit_hold as ph
from mexc_tick_scalper.execution import OrderSide


def test_profit_hold_changes_only_after_first_positive_tick() -> None:
    args = SimpleNamespace(
        mid_adverse_cut_bps=0.01,
        leader_retrace_exit_bps=1.5,
        reversal_edge_bps=0.75,
        no_progress_ms=3000,
        max_hold_ms=15000,
        profit_runner_arm_bps=5.0,
        min_absolute_residual_bps=8.0,
        min_signal_strength_ratio=3.0,
    )
    old_args = ph._ACTIVE_ARGS
    old_policy = ph._SAVED_PRE_PROFIT_POLICY
    try:
        ph._ACTIVE_ARGS = args
        ph._SAVED_PRE_PROFIT_POLICY = {}
        trail = ph.ProfitHoldTrailing(distance_bps=1.5)

        # Losing/flat position keeps the exact original policy.
        assert trail.update(-1.0) is None
        assert args.mid_adverse_cut_bps == 0.01
        assert args.leader_retrace_exit_bps == 1.5
        assert args.reversal_edge_bps == 0.75
        assert args.no_progress_ms == 3000
        assert args.max_hold_ms == 15000
        assert args.min_absolute_residual_bps == 8.0
        assert args.min_signal_strength_ratio == 3.0

        # First positive executable tick arms winner hold and a positive floor.
        stop = trail.update(0.20)
        assert stop is not None and 0.0 < stop <= 0.20

        # Hard adverse protection remains active.
        assert args.mid_adverse_cut_bps == 0.01

        # Lead-lag thesis/lifecycle exits no longer cut the profitable winner.
        assert math.isinf(args.leader_retrace_exit_bps)
        assert math.isinf(args.reversal_edge_bps)
        assert args.no_progress_ms > 1_000_000
        assert args.max_hold_ms > 1_000_000

        # Entry thresholds were never touched.
        assert args.min_absolute_residual_bps == 8.0
        assert args.min_signal_strength_ratio == 3.0
    finally:
        ph._ACTIVE_ARGS = old_args
        ph._SAVED_PRE_PROFIT_POLICY = old_policy


def test_profit_hold_preserves_original_trailing_ratchet() -> None:
    old_args = ph._ACTIVE_ARGS
    old_policy = ph._SAVED_PRE_PROFIT_POLICY
    try:
        ph._ACTIVE_ARGS = None
        ph._SAVED_PRE_PROFIT_POLICY = {}
        trail = ph.ProfitHoldTrailing(distance_bps=1.5)
        assert trail.update(3.0) == 0.5
        assert trail.update(5.0) == 2.0
        assert trail.update(10.0) == 8.5
        assert trail.update(9.0) == 8.5
    finally:
        ph._ACTIVE_ARGS = old_args
        ph._SAVED_PRE_PROFIT_POLICY = old_policy


def test_network_only_order_poll_adds_no_asyncio_sleep(monkeypatch) -> None:
    class FakeAdapter:
        calls = 0

        async def _get_order_by_external_id(self, symbol, client_order_id):
            self.calls += 1
            if self.calls == 1:
                return {"state": 1}
            return {"state": 3, "dealVol": 1}

    async def forbidden_sleep(*args, **kwargs):
        raise AssertionError("network-only order polling must not call asyncio.sleep")

    monkeypatch.setattr(asyncio, "sleep", forbidden_sleep)
    adapter = FakeAdapter()
    result = asyncio.run(
        ph._network_only_wait_for_order_result(adapter, "XRP_USDT", "test-order", 1.0)
    )
    assert result["state"] == 3
    assert adapter.calls == 2


def test_confirmed_fill_starts_position_management_without_get_positions() -> None:
    class Fill:
        filled_qty = 10.0
        avg_price = 1.2345
        position_id = "p1"

    class FakeAdapter:
        async def get_positions(self, symbol):
            raise AssertionError("get_positions must not block management after confirmed fill")

    result = asyncio.run(
        ph._immediate_position_from_fill(
            FakeAdapter(), "XRP_USDT", OrderSide.LONG, Fill(), 200
        )
    )
    assert result.symbol == "XRP_USDT"
    assert result.side is OrderSide.LONG
    assert result.qty == 10.0
    assert result.entry_price == 1.2345
    assert result.leverage == 200
    assert result.position_id == "p1"
