from mexc_tick_scalper.execution import OrderSide
from mexc_tick_scalper.testnet_latency_arb_safe import _parse_risk_limits, _position_type


def test_position_type_is_side_specific():
    assert _position_type(OrderSide.LONG) == 1
    assert _position_type(OrderSide.SHORT) == 2


def test_parse_private_risk_limit_uses_account_maxvol():
    response = {
        "success": True,
        "code": 0,
        "data": {
            "ARB_USDT": [
                {
                    "symbol": "ARB_USDT",
                    "positionType": 1,
                    "level": 1,
                    "maxVol": 12345,
                    "maxLeverage": 20,
                    "mmr": 0.005,
                    "imr": 0.05,
                },
                {
                    "symbol": "ARB_USDT",
                    "positionType": 2,
                    "level": 1,
                    "maxVol": 6789,
                    "maxLeverage": 10,
                    "mmr": 0.006,
                    "imr": 0.1,
                },
            ]
        },
    }
    rows = _parse_risk_limits(response)
    assert rows[("ARB_USDT", 1)].max_vol == 12345
    assert rows[("ARB_USDT", 1)].max_leverage == 20
    assert rows[("ARB_USDT", 2)].max_vol == 6789
    assert rows[("ARB_USDT", 2)].max_leverage == 10


def test_zero_private_capacity_is_preserved_as_zero():
    response = {
        "data": {
            "TEST_USDT": [
                {
                    "symbol": "TEST_USDT",
                    "positionType": 1,
                    "maxVol": 0,
                    "maxLeverage": 10,
                    "mmr": 0.01,
                    "imr": 0.1,
                }
            ]
        }
    }
    rows = _parse_risk_limits(response)
    assert rows[("TEST_USDT", 1)].max_vol == 0
