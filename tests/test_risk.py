from mexc_tick_scalper.risk import confidence_from_profit_factor, make_position_plan


def test_profit_factor_controls_size():
    assert confidence_from_profit_factor(1.20) == 0.0
    assert confidence_from_profit_factor(1.40) == 0.25
    assert confidence_from_profit_factor(1.60) == 0.50
    assert confidence_from_profit_factor(1.90) == 0.75
    assert confidence_from_profit_factor(2.30) == 1.0


def test_position_plan_respects_margin_and_leverage_caps():
    plan = make_position_plan(
        price=100.0,
        bankroll_usdt=200.0,
        base_margin_fraction=0.03,
        max_margin_per_trade_usdt=10.0,
        hard_max_leverage=100,
        exchange_max_leverage=200,
        stress_move_bps=50.0,
        validated_profit_factor=2.30,
    )
    assert plan is not None
    assert plan.margin_usdt == 6.0
    assert 1 <= plan.leverage <= 100
    assert plan.target_notional_usdt == plan.margin_usdt * plan.leverage
    assert plan.qty == plan.target_notional_usdt / 100.0


def test_no_plan_without_edge():
    plan = make_position_plan(
        price=100.0,
        bankroll_usdt=200.0,
        base_margin_fraction=0.03,
        max_margin_per_trade_usdt=10.0,
        hard_max_leverage=100,
        exchange_max_leverage=100,
        stress_move_bps=20.0,
        validated_profit_factor=1.10,
    )
    assert plan is None
