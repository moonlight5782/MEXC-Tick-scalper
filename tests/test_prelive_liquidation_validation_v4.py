from mexc_tick_scalper.prelive_liquidation_validation_v4 import FixedInitialMarginBudget


def test_fixed_initial_margin_budget_caps_at_configured_margin():
    budget = FixedInitialMarginBudget(50.0, 100.0)
    assert 100.0 * budget == 50.0
    assert 125.0 * budget == 50.0


def test_fixed_initial_margin_budget_caps_at_remaining_balance():
    budget = FixedInitialMarginBudget(50.0, 100.0)
    assert 40.0 * budget == 40.0


def test_requested_notional_cap_matches_isolated_margin_times_leverage():
    budget = FixedInitialMarginBudget(50.0, 100.0)
    balance = 100.0
    leverage = 20
    frozen_target_notional = 10_000.0
    cap = balance * budget * leverage
    assert cap == 1_000.0
    assert min(frozen_target_notional, cap) == 1_000.0


def test_max_leverage_still_does_not_imply_full_bank_margin():
    budget = FixedInitialMarginBudget(50.0, 100.0)
    balance = 100.0
    leverage = 125
    cap = balance * budget * leverage
    assert cap == 6_250.0
