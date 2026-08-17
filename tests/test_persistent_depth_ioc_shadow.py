from pathlib import Path

import pytest

from mexc_tick_scalper.microspread_feed import LiveBook
from mexc_tick_scalper.prelive_persistent_ioc_shadow import virtual_ioc_fill
from mexc_tick_scalper.prelive_persistent_ioc_shadow_v2 import Stats, entry_slippage_bps


def book(*, bid=99.0, ask=100.0, bids=((99.0, 100.0),), asks=((100.0, 100.0),), recv_ms=1):
    return LiveBook(
        bid=bid,
        ask=ask,
        recv_ms=recv_ms,
        exchange_ts_ms=recv_ms,
        bids=tuple(bids),
        asks=tuple(asks),
    )


def test_arrival_book_ioc_uses_current_liquidity_only():
    current = book(asks=((100.0, 2.0), (100.005, 3.0), (100.02, 50.0)), recv_ms=20)
    fill = virtual_ioc_fill(
        current,
        direction=1,
        target_notional_usdt=1000.0,
        contract_size=1.0,
        cross_bps=1.0,
    )
    assert fill.qty == pytest.approx(5.0)
    assert fill.avg_price == pytest.approx((2 * 100.0 + 3 * 100.005) / 5)
    assert fill.fill_ratio == pytest.approx(0.5)
    assert entry_slippage_bps(1, current, fill.avg_price) <= 1.0


def test_ioc_does_not_cross_beyond_slippage_limit():
    current = book(asks=((100.0, 1.0), (100.02, 10.0)), recv_ms=20)
    fill = virtual_ioc_fill(
        current,
        direction=1,
        target_notional_usdt=1000.0,
        contract_size=1.0,
        cross_bps=1.0,
    )
    assert fill.qty == pytest.approx(1.0)
    assert fill.avg_price == pytest.approx(100.0)


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
    assert "previous.recv_ms < pending.arrival_wall_ms" not in text
