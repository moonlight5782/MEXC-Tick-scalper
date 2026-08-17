from mexc_tick_scalper.microspread_feed import LiveBook
from mexc_tick_scalper.prelive_persistent_ioc_shadow import virtual_ioc_fill
from mexc_tick_scalper.prelive_persistent_ioc_shadow_v2 import build_parser, entry_slippage_bps


def _book():
    return LiveBook(
        bid=99.99,
        ask=100.00,
        recv_ms=1,
        exchange_ts_ms=1,
        bids=((99.99, 100.0),),
        asks=((100.00, 2.0), (100.005, 3.0), (100.02, 100.0)),
    )


def test_ioc_uses_only_liquidity_inside_live_limit():
    book = _book()
    fill = virtual_ioc_fill(
        book,
        direction=1,
        target_notional_usdt=10_000.0,
        contract_size=1.0,
        cross_bps=1.0,
    )
    # 100.02 is outside a 1bp IOC limit from the 100.00 best ask.
    assert fill.qty == 5.0
    assert fill.fill_ratio < 0.10
    assert entry_slippage_bps(1, book, fill.avg_price) <= 1.0


def test_no_extra_depth_wait_option_remains():
    args = build_parser().parse_args([])
    assert not hasattr(args, "arrival_book_wait_ms")
    assert args.ioc_cross_bps == 1.0
    assert args.max_entry_slippage_bps == 1.0
