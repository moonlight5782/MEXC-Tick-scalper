from pathlib import Path

import pytest

from mexc_tick_scalper.microspread_feed import LiveBook
from mexc_tick_scalper.prelive_persistent_ioc_shadow_v2 import Stats, persistent_ioc_fill


def book(*, bid=99.0, ask=100.0, bids=((99.0, 100.0),), asks=((100.0, 100.0),), recv_ms=1):
    return LiveBook(
        bid=bid,
        ask=ask,
        recv_ms=recv_ms,
        exchange_ts_ms=recv_ms,
        bids=tuple(bids),
        asks=tuple(asks),
    )


def test_persistent_ioc_caps_fill_to_liquidity_visible_in_both_books():
    previous = book(asks=((100.0, 2.0),), recv_ms=10)
    current = book(asks=((100.0, 5.0),), recv_ms=20)
    fill = persistent_ioc_fill(
        previous,
        current,
        direction=1,
        target_notional_usdt=1000.0,
        contract_size=1.0,
        cross_bps=1.0,
    )
    assert fill.qty == pytest.approx(2.0)
    assert fill.avg_price == pytest.approx(100.0)
    assert fill.fill_ratio == pytest.approx(0.2)


def test_persistent_ioc_rejects_liquidity_that_disappears():
    previous = book(asks=((100.0, 0.0), (101.0, 5.0)), recv_ms=10)
    current = book(asks=((100.0, 5.0),), recv_ms=20)
    fill = persistent_ioc_fill(
        previous,
        current,
        direction=1,
        target_notional_usdt=1000.0,
        contract_size=1.0,
        cross_bps=1.0,
    )
    assert fill.qty == 0.0


def test_profit_factor_is_usdt_weighted():
    s = Stats(gross_win_usdt=10.0, gross_loss_usdt=4.0)
    assert s.pf == pytest.approx(2.5)


def test_runner_is_structurally_paper_only():
    source = Path(__file__).parents[1] / "src" / "mexc_tick_scalper" / "prelive_persistent_ioc_shadow_v2.py"
    text = source.read_text(encoding="utf-8")
    assert "write_enabled=True" not in text
    assert "open_ioc(" not in text
    assert "close_market_reduce_only(" not in text
    assert "cancel_order(" not in text
