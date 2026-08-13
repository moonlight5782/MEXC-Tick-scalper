import asyncio

import pytest

import mexc_tick_scalper.demo_live_signal_test as live_demo
from mexc_tick_scalper.execution import OrderFill, OrderSide
from mexc_tick_scalper.hybrid_strategy import MicrostructureSnapshot
from mexc_tick_scalper.orderbook_signal import OrderBookFeatures, book_confirmation
from mexc_tick_scalper.web_execution import MexcWebError, MexcWebExecutionAdapter, WebExecutionConfig


def _demo_config() -> WebExecutionConfig:
    return WebExecutionConfig(
        auth_token="WEB_test",
        base_url="https://futures.testnet.mexc.com/api/v1",
        origin="https://futures.testnet.mexc.com",
        referer="https://futures.testnet.mexc.com/futures/TEST_USDT",
        write_enabled=True,
        environment="demo",
    )


def _snap() -> MicrostructureSnapshot:
    return MicrostructureSnapshot(1, 0.5, 5.0, 0.6, 0.2, 0.5, 5)


def test_reduce_only_retries_transient_missing_position(monkeypatch):
    calls = 0

    async def flaky_close(self, *args, **kwargs):
        nonlocal calls
        calls += 1
        if calls < 3:
            raise MexcWebError("no open position for TEST_USDT")
        return OrderFill(
            symbol="TEST_USDT",
            side=OrderSide.SHORT,
            requested_qty=1.0,
            filled_qty=1.0,
            avg_price=100.0,
            fee_usdt=0.0,
            order_id="close-1",
            client_order_id="client-1",
        )

    monkeypatch.setattr(MexcWebExecutionAdapter, "close_market_reduce_only", flaky_close)
    monkeypatch.setattr(live_demo, "CLOSE_POSITION_RETRY_SECONDS", 0.0)

    adapter = live_demo._GuardedDemoAdapter(_demo_config())
    fill = asyncio.run(
        adapter.close_market_reduce_only(
            symbol="TEST_USDT",
            qty=1.0,
            side=OrderSide.SHORT,
            client_order_id="client-1",
        )
    )

    assert calls == 3
    assert fill.filled_qty == 1.0


def test_reduce_only_does_not_retry_unrelated_errors(monkeypatch):
    calls = 0

    async def broken_close(self, *args, **kwargs):
        nonlocal calls
        calls += 1
        raise MexcWebError("HTTP 500 from order submit")

    monkeypatch.setattr(MexcWebExecutionAdapter, "close_market_reduce_only", broken_close)
    adapter = live_demo._GuardedDemoAdapter(_demo_config())

    with pytest.raises(MexcWebError, match="HTTP 500"):
        asyncio.run(
            adapter.close_market_reduce_only(
                symbol="TEST_USDT",
                qty=1.0,
                side=OrderSide.SHORT,
                client_order_id="client-1",
            )
        )

    assert calls == 1


def test_spread_aware_policy_never_uses_trail_narrower_than_recent_demo_spread(monkeypatch):
    monkeypatch.setattr(live_demo, "_LAST_DEMO_SPREAD_BPS", 8.0)
    policy = live_demo._SpreadAwareExitPolicy(
        side=1,
        entry_price=100.0,
        winner_arm_bps=1.0,
        winner_pullback_bps=2.0,
    )

    assert policy.on_tick(
        price=100.10,
        liquidation_price=None,
        signal=_snap(),
        age_seconds=1.0,
    ) is None

    assert policy.trailing_distance_bps == 8.0
    assert policy.trailing_stop_bps == pytest.approx(2.0)


def test_default_live_l2_veto_blocks_moderate_opposite_pressure():
    book = OrderBookFeatures(
        bid=100.0,
        ask=100.1,
        mid=100.05,
        spread_bps=9.995,
        imbalance=0.58,
        microprice=100.05,
        microprice_edge_bps=0.0,
        bid_depth=158.0,
        ask_depth=42.0,
        pressure=0.392,
        direction=1,
        confidence=0.392,
    )

    decision = book_confirmation(
        trade_direction=-1,
        book=book,
        veto_confidence=live_demo.DEFAULT_BOOK_VETO_CONFIDENCE,
        confirm_confidence=live_demo.DEFAULT_BOOK_CONFIRM_CONFIDENCE,
    )

    assert live_demo.DEFAULT_BOOK_VETO_CONFIDENCE == 0.30
    assert not decision.allowed
    assert decision.reason == "strong_book_disagreement"
