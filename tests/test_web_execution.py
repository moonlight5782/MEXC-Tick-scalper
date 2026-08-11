import asyncio

import pytest

from mexc_tick_scalper.web_execution import (
    MexcWebError,
    MexcWebExecutionAdapter,
    WebExecutionConfig,
    _signature,
)


def test_signature_is_deterministic_for_same_payload_and_timestamp():
    token = "WEB_test_token"
    payload = {"symbol": "BTC_USDT", "price": 100.0, "vol": 1, "side": 1, "type": 3}
    first = _signature(token, payload, 1720000000000)
    second = _signature(token, payload, 1720000000000)
    assert first == second
    assert len(first) == 32


def test_write_disabled_by_default():
    adapter = MexcWebExecutionAdapter(WebExecutionConfig(auth_token="WEB_test"))
    with pytest.raises(MexcWebError, match="writes are disabled"):
        adapter._require_write()


def test_write_can_be_explicitly_enabled():
    adapter = MexcWebExecutionAdapter(WebExecutionConfig(auth_token="WEB_test", write_enabled=True))
    adapter._require_write()


def test_base_qty_is_converted_to_contract_volume():
    async def scenario():
        adapter = MexcWebExecutionAdapter(WebExecutionConfig(auth_token="WEB_test"))
        adapter._contract_cache["TEST_USDT"] = {
            "symbol": "TEST_USDT",
            "contractSize": 0.01,
            "volUnit": 1,
            "minVol": 1,
        }
        assert await adapter._to_contract_vol("TEST_USDT", 1.239) == 123
        assert await adapter._from_contract_vol("TEST_USDT", 123) == pytest.approx(1.23)

    asyncio.run(scenario())
