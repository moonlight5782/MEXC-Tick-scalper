import pytest

import mexc_tick_scalper.demo_hybrid_test as hybrid
import mexc_tick_scalper.demo_live_signal_test as live_demo
from mexc_tick_scalper.execution import OrderFill, OrderSide
from mexc_tick_scalper.hybrid_strategy import MicrostructureSnapshot
from mexc_tick_scalper.orderbook_signal import OrderBookFeatures, book_confirmation


def test_strict_book_guard_rejects_neutral_book():
    book = OrderBookFeatures(
        bid=100.0,
        ask=100.1,
        mid=100.05,
        spread_bps=9.995,
        imbalance=0.10,
        microprice=100.05,
        microprice_edge_bps=0.0,
        bid_depth=110.0,
        ask_depth=90.0,
        pressure=0.065,
        direction=1,
        confidence=0.065,
    )
    decision = book_confirmation(
        trade_direction=1,
        book=book,
        veto_confidence=live_demo.DEFAULT_BOOK_VETO_CONFIDENCE,
        confirm_confidence=live_demo.DEFAULT_BOOK_CONFIRM_CONFIDENCE,
    )
    assert decision.allowed
    assert decision.reason == "book_neutral"
    assert not live_demo._strict_book_allows(decision, True)


def test_momentum_guard_blocks_opposite_price_momentum(monkeypatch):
    raw = MicrostructureSnapshot(
        direction=-1,
        confidence=0.80,
        trade_rate=10.0,
        buy_ratio=0.10,
        cvd_norm=-0.80,
        momentum_bps=0.50,
        price_changes=8,
    )
    monkeypatch.setattr(hybrid.MicrostructureSignal, "update", lambda self, tick: raw)
    signal = live_demo._MomentumAlignedSignal(window_seconds=5.0, min_trade_rate=0.5)
    filtered = signal.update(object())
    assert filtered.direction == 0
    assert filtered.momentum_bps == pytest.approx(0.50)


def test_momentum_guard_keeps_aligned_direction(monkeypatch):
    raw = MicrostructureSnapshot(
        direction=-1,
        confidence=0.80,
        trade_rate=10.0,
        buy_ratio=0.10,
        cvd_norm=-0.80,
        momentum_bps=-0.50,
        price_changes=8,
    )
    monkeypatch.setattr(hybrid.MicrostructureSignal, "update", lambda self, tick: raw)
    signal = live_demo._MomentumAlignedSignal(window_seconds=5.0, min_trade_rate=0.5)
    filtered = signal.update(object())
    assert filtered.direction == -1


def test_pending_fill_downgrades_to_nonfatal_zero_fill_path():
    fill = OrderFill(
        symbol="TEST_USDT",
        side=OrderSide.LONG,
        requested_qty=1.0,
        filled_qty=0.7,
        avg_price=100.0,
        fee_usdt=0.0,
        order_id="entry-1",
        client_order_id="client-1",
        position_id="position-1",
    )
    pending = live_demo._as_pending_fill(fill)
    assert pending.filled_qty == 0.0
    assert pending.order_id == fill.order_id
    assert pending.position_id == fill.position_id
