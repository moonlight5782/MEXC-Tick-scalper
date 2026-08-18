from mexc_tick_scalper.canonical_execution import ExecutionState, RiskCap, parse_risk_limits


def test_parse_risk_limits_keeps_side_specific_private_caps():
    payload = {
        "data": {
            "BTC_USDT": [
                {"symbol": "BTC_USDT", "positionType": 1, "maxVol": 12, "maxLeverage": 20, "mmr": 0.004, "imr": 0.05},
                {"symbol": "BTC_USDT", "positionType": 2, "maxVol": 7, "maxLeverage": 10, "mmr": 0.005, "imr": 0.10},
            ]
        }
    }
    caps = parse_risk_limits(payload)
    assert caps[("BTC_USDT", 1)] == RiskCap("BTC_USDT", 1, 12.0, 20, 0.004, 0.05)
    assert caps[("BTC_USDT", 2)] == RiskCap("BTC_USDT", 2, 7.0, 10, 0.005, 0.10)


def test_execution_state_machine_has_no_implicit_retry_state():
    assert [state.value for state in ExecutionState] == [
        "flat", "entry_pending", "open", "exit_pending", "reconciling"
    ]
