import asyncio

import pytest

from mexc_tick_scalper.web_execution import (
    MexcWebError,
    MexcWebExecutionAdapter,
    WebExecutionConfig,
    _json_body,
    _signature,
)
from mexc_tick_scalper.execution import OrderSide, PositionSnapshot


def test_signature_is_deterministic_for_same_payload_and_timestamp():
    token = "WEB_test_token"
    payload = {"symbol": "BTC_USDT", "price": 100.0, "vol": 1, "side": 1, "type": 3}
    first = _signature(token, payload, 1720000000000)
    second = _signature(token, payload, 1720000000000)
    assert first == second
    assert len(first) == 32


def test_json_body_matches_browser_integer_number_shape():
    body = _json_body({"price": 100.0, "vol": 1.0, "fraction": 1.25})
    assert body == '{"price":100,"vol":1,"fraction":1.25}'


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


def test_exact_hedge_leg_close_includes_position_id(monkeypatch):
    payloads = []

    async def fake_request(self, method, path, *, params=None, payload=None):
        payloads.append(payload)
        return {"data": "submitted"}

    async def fake_result(self, symbol, external_id, timeout_seconds=1.2):
        return {"dealVol": 1, "dealAvgPrice": 100.0, "orderId": "close-1"}

    monkeypatch.setattr(MexcWebExecutionAdapter, "_request", fake_request)
    monkeypatch.setattr(MexcWebExecutionAdapter, "_wait_for_order_result", fake_result)
    cfg = WebExecutionConfig(
        auth_token="WEB_test", base_url="https://futures.testnet.mexc.com/api/v1",
        origin="https://futures.testnet.mexc.com",
        referer="https://futures.testnet.mexc.com/futures/TEST_USDT",
        write_enabled=True, environment="demo",
    )
    adapter = MexcWebExecutionAdapter(cfg)
    adapter._contract_cache["TEST_USDT"] = {
        "symbol": "TEST_USDT", "contractSize": 1, "volUnit": 1, "minVol": 1,
        "priceUnit": 0.1,
    }
    position = PositionSnapshot(
        symbol="TEST_USDT", side=OrderSide.SHORT, qty=1.0, entry_price=100.0,
        leverage=10, isolated=True, position_id="short-leg-42",
    )

    asyncio.run(adapter.close_position_snapshot_reduce_only(position, client_order_id="cleanup-1"))

    assert payloads[0]["positionId"] == "short-leg-42"
    assert payloads[0]["side"] == 2


def test_exact_close_falls_back_to_ioc_limit_when_market_crosses_liquidation(monkeypatch):
    posts = []

    async def fake_request(self, method, path, *, params=None, payload=None):
        if method == "POST":
            posts.append(dict(payload))
            if len(posts) == 1:
                raise MexcWebError("code=2078 message=Fill price exceeds the liquidation price")
            return {"data": "submitted"}
        return {"data": {"asks": [[100.0, 1]], "bids": [[99.9, 1]]}}

    async def fake_result(self, symbol, external_id, timeout_seconds=1.2):
        return {"dealVol": 1, "dealAvgPrice": 100.1, "orderId": "close-limit"}

    monkeypatch.setattr(MexcWebExecutionAdapter, "_request", fake_request)
    monkeypatch.setattr(MexcWebExecutionAdapter, "_wait_for_order_result", fake_result)
    cfg = WebExecutionConfig(
        auth_token="WEB_test", base_url="https://futures.testnet.mexc.com/api/v1",
        origin="https://futures.testnet.mexc.com",
        referer="https://futures.testnet.mexc.com/futures/TEST_USDT",
        write_enabled=True, environment="demo",
    )
    adapter = MexcWebExecutionAdapter(cfg)
    adapter._contract_cache["TEST_USDT"] = {
        "symbol": "TEST_USDT", "contractSize": 1, "volUnit": 1, "minVol": 1,
        "priceUnit": 0.1,
    }
    position = PositionSnapshot(
        symbol="TEST_USDT", side=OrderSide.SHORT, qty=1.0, entry_price=100.0,
        leverage=200, isolated=True, position_id="short-max",
    )

    asyncio.run(adapter.close_position_snapshot_reduce_only(position, client_order_id="cleanup-2078"))

    assert posts[0]["type"] == 5
    assert posts[1]["type"] == 3
    assert posts[1]["positionId"] == "short-max"
    assert posts[1]["price"] > 100.0
