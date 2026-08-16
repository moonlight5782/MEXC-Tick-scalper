from pathlib import Path

import pytest

from mexc_tick_scalper.microspread_feed import LiveBook
from mexc_tick_scalper.prelive_latency_diagnostic import _exit_depth_for_qty, _required_edge, _walk_depth


class Args:
    min_edge_bps = 0.35
    min_net_edge_bps = 0.20
    edge_to_spread_ratio = 1.15


def _book() -> LiveBook:
    return LiveBook(
        bid=99.9,
        ask=100.0,
        recv_ms=1,
        exchange_ts_ms=1,
        bids=((99.9, 10.0), (99.8, 10.0)),
        asks=((100.0, 5.0), (100.1, 10.0)),
    )


def test_required_edge_includes_spread_and_buffer():
    assert _required_edge(0.8, Args()) == pytest.approx(1.0)


def test_walk_depth_long_entry_uses_asks_and_partial_ioc():
    qty, vwap = _walk_depth(
        _book(), direction=1, target_notional_usdt=2000.0, contract_size=1.0, opening=True,
    )
    # Only 15 base units are visible on asks, so IOC must remain partial.
    assert qty == pytest.approx(15.0)
    assert vwap == pytest.approx((5 * 100.0 + 10 * 100.1) / 15.0)


def test_walk_depth_short_entry_uses_bids():
    qty, vwap = _walk_depth(
        _book(), direction=-1, target_notional_usdt=1000.0, contract_size=1.0, opening=True,
    )
    assert qty > 10.0
    assert vwap < 99.9


def test_exit_depth_long_hits_bids():
    filled, vwap = _exit_depth_for_qty(_book(), direction=1, qty=15.0, contract_size=1.0)
    assert filled == pytest.approx(15.0)
    assert vwap == pytest.approx((10 * 99.9 + 5 * 99.8) / 15.0)


def test_diagnostic_module_never_enables_live_writes():
    source = Path("src/mexc_tick_scalper/prelive_latency_diagnostic.py").read_text(encoding="utf-8")
    assert "write_enabled=True" not in source
    assert ".open_ioc(" not in source
    assert "close_market_reduce_only" not in source
    assert "close_position_snapshot_reduce_only" not in source
