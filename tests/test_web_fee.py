from mexc_tick_scalper.web_fee import parse_fee_status


def test_zero_fee_is_confirmed_only_when_both_are_zero():
    status = parse_fee_status(
        {"data": [{"symbol": "UNI_USDT", "makerFeeRate": 0, "takerFeeRate": 0}]},
        "UNI_USDT",
    )
    assert status.zero_confirmed


def test_nonzero_taker_blocks_symbol():
    status = parse_fee_status(
        {"data": [{"symbol": "UNI_USDT", "makerFeeRate": 0, "takerFeeRate": 0.0001}]},
        "UNI_USDT",
    )
    assert not status.zero_confirmed
    assert status.taker == 0.0001


def test_missing_symbol_is_unknown_and_blocked():
    status = parse_fee_status({"data": []}, "UNI_USDT")
    assert not status.zero_confirmed
    assert status.maker is None
    assert status.taker is None
