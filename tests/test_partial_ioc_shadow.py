from mexc_tick_scalper.microspread_feed import LiveBook
from mexc_tick_scalper.prelive_persistent_ioc_shadow import (
    executable_entry_edge_ok,
    immediate_roundtrip_cost_bps,
    virtual_ioc_fill,
)


def _book():
    return LiveBook(
        bid=99.98,
        ask=100.00,
        recv_ms=1,
        exchange_ts_ms=1,
        bids=((99.98, 5.0), (99.97, 20.0)),
        asks=((100.00, 2.0), (100.005, 2.0), (100.02, 100.0)),
    )


def test_ioc_limit_accepts_partial_fill_and_cancels_remainder():
    fill = virtual_ioc_fill(
        _book(), direction=1, target_notional_usdt=10_000.0, contract_size=1.0, cross_bps=1.0
    )
    # 100.02 is outside a 1 bps IOC cap from best ask=100.00, so only 4 base fill.
    assert fill.qty == 4.0
    assert 400.0 <= fill.qty * fill.avg_price < 401.0
    assert fill.fill_ratio < 0.05
    assert fill.avg_price < 100.01


def test_roundtrip_cost_is_calculated_on_actual_partial_qty():
    fill = virtual_ioc_fill(
        _book(), direction=1, target_notional_usdt=10_000.0, contract_size=1.0, cross_bps=1.0
    )
    cost = immediate_roundtrip_cost_bps(
        _book(), direction=1, entry_price=fill.avg_price, qty=fill.qty, contract_size=1.0
    )
    assert cost > 0
    assert cost < 10


def test_executable_edge_must_cover_roundtrip_cost_and_buffer():
    ok, required = executable_entry_edge_ok(
        residual_bps=12.0,
        roundtrip_cost_bps=6.0,
        min_net_edge_bps=2.0,
        min_edge_to_cost_ratio=1.5,
    )
    assert ok
    assert required == 9.0

    ok, required = executable_entry_edge_ok(
        residual_bps=8.0,
        roundtrip_cost_bps=6.0,
        min_net_edge_bps=2.0,
        min_edge_to_cost_ratio=1.5,
    )
    assert not ok
    assert required == 9.0
