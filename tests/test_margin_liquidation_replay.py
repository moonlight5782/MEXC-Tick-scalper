from mexc_tick_scalper.margin_liquidation_replay import (
    ContractRisk,
    LoggedTrade,
    liquidation_distance_bps,
    liquidation_price,
    parse_log,
    replay,
)


def test_liquidation_distance_and_price_for_max_leverage() -> None:
    # 200x isolated with 0.4% MMR leaves about 0.1% / 10 bps before liquidation.
    assert abs(liquidation_distance_bps(200, 0.004) - 10.0) < 1e-9
    assert abs(liquidation_price(100.0, 1, 200, 0.004) - 99.9) < 1e-9
    assert abs(liquidation_price(100.0, -1, 200, 0.004) - 100.1) < 1e-9


def test_parse_log_pairs_entry_and_exit() -> None:
    text = """
ENTRY BTW_USDT LONG requested=$10000 filled=$2082 (20.8%) spread=1.50bps slip=0.98bps residual=+22.58bps cost=2.48bps
EXIT BTW_USDT mexc_catchup_convergence pnl=+18.90bps $+3.94 hold=78ms
"""
    rows = parse_log(text)
    assert len(rows) == 1
    assert rows[0].symbol == "BTW_USDT"
    assert rows[0].direction == 1
    assert rows[0].recorded_notional == 2082
    assert rows[0].exit_pnl_bps == 18.90


def test_replay_caps_notional_by_margin_and_marks_liquidation_lower_bound() -> None:
    trades = [
        LoggedTrade("X_USDT", 1, 10_000.0, "leader_retrace", -15.0),
        LoggedTrade("X_USDT", -1, 10_000.0, "catchup", 20.0),
    ]
    risks = {"X_USDT": ContractRisk("X_USDT", 200, 0.004)}
    rows = replay(
        trades,
        risks,
        starting_balance_usdt=100.0,
        initial_margin_usdt=25.0,
        requested_leverage=0,
    )
    assert len(rows) == 2
    assert rows[0].leverage == 200
    assert rows[0].notional == 5000.0
    assert rows[0].initial_margin == 25.0
    assert rows[0].liquidated_by_exit_lower_bound is True
    assert abs(rows[0].pnl_usdt + 5.0) < 1e-9
    assert abs(rows[0].balance_after - 95.0) < 1e-9
    assert rows[1].liquidated_by_exit_lower_bound is False
    assert abs(rows[1].pnl_usdt - 10.0) < 1e-9
    assert abs(rows[1].balance_after - 105.0) < 1e-9


def test_user_leverage_is_clamped_to_contract_max() -> None:
    trades = [LoggedTrade("X_USDT", 1, 1000.0, "catchup", 5.0)]
    risks = {"X_USDT": ContractRisk("X_USDT", 75, 0.005)}
    rows = replay(
        trades,
        risks,
        starting_balance_usdt=100.0,
        initial_margin_usdt=20.0,
        requested_leverage=200,
    )
    assert rows[0].leverage == 75
    assert rows[0].notional == 1000.0
