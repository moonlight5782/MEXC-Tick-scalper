from argparse import Namespace

import pytest

from mexc_tick_scalper.execution import OrderSide
from mexc_tick_scalper.live_production_runner import (
    _assert_live_write_config,
    _marketable_ioc_price,
    _required_edge,
)
from mexc_tick_scalper.microspread_feed import LiveBook
from mexc_tick_scalper.web_execution import MexcWebError, WebExecutionConfig


def test_live_runner_requires_explicit_write_unlock(monkeypatch):
    monkeypatch.delenv("MEXC_LIVE_WRITE", raising=False)
    cfg = WebExecutionConfig(auth_token="WEB_test", write_enabled=True, environment="live")
    with pytest.raises(MexcWebError, match="MEXC_LIVE_WRITE=YES"):
        _assert_live_write_config(cfg)


def test_live_runner_accepts_exact_live_host_when_unlocked(monkeypatch):
    monkeypatch.setenv("MEXC_LIVE_WRITE", "YES")
    cfg = WebExecutionConfig(auth_token="WEB_test", write_enabled=True, environment="live")
    _assert_live_write_config(cfg)


def test_live_runner_rejects_non_live_host(monkeypatch):
    monkeypatch.setenv("MEXC_LIVE_WRITE", "YES")
    cfg = WebExecutionConfig(
        auth_token="WEB_test",
        base_url="https://futures.testnet.mexc.com/api/v1",
        write_enabled=True,
        environment="live",
    )
    with pytest.raises(MexcWebError, match="refuses host"):
        _assert_live_write_config(cfg)


def test_required_edge_covers_live_spread_and_buffer():
    args = Namespace(min_edge_bps=0.35, min_net_edge_bps=0.20, edge_to_spread_ratio=1.05)
    assert _required_edge(0.10, args) == pytest.approx(0.35)
    assert _required_edge(0.80, args) == pytest.approx(1.00)
    assert _required_edge(2.00, args) == pytest.approx(2.20)


def test_marketable_ioc_crosses_live_top_with_tick_rounding():
    book = LiveBook(bid=99.9, ask=100.0, recv_ms=1, exchange_ts_ms=1)
    long_px = _marketable_ioc_price(OrderSide.LONG, book, cross_bps=1.0, price_unit=0.01)
    short_px = _marketable_ioc_price(OrderSide.SHORT, book, cross_bps=1.0, price_unit=0.01)
    assert long_px >= book.ask
    assert short_px <= book.bid
    assert long_px == pytest.approx(100.01)
    assert short_px == pytest.approx(99.89)
