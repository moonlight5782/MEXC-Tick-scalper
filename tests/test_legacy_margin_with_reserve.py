import mexc_tick_scalper.auto_discovery_shadow as auto
import mexc_tick_scalper.btw_economic_arrival_shadow as btw


def test_auto_legacy_margin_targets_50_usdt_at_100_bank():
    old_balance = auto.BANK.balance_usdt
    old_contracts = dict(auto.CONTRACTS)
    old_symbol = auto.CURRENT_SYMBOL
    try:
        auto.BANK.balance_usdt = 100.0
        auto.CURRENT_SYMBOL = "TEST_USDT"
        auto.CONTRACTS["TEST_USDT"] = type("C", (), {"max_leverage": 200})()
        requested, margin, reserve = auto._requested_notional_and_margin("TEST_USDT")
        assert margin == 50.0
        assert reserve == 50.0
        assert requested == 10_000.0
    finally:
        auto.BANK.balance_usdt = old_balance
        auto.CONTRACTS.clear()
        auto.CONTRACTS.update(old_contracts)
        auto.CURRENT_SYMBOL = old_symbol


def test_auto_legacy_margin_never_uses_entire_smaller_bank():
    old_balance = auto.BANK.balance_usdt
    old_contracts = dict(auto.CONTRACTS)
    try:
        auto.BANK.balance_usdt = 60.0
        auto.CONTRACTS["TEST_USDT"] = type("C", (), {"max_leverage": 200})()
        requested, margin, reserve = auto._requested_notional_and_margin("TEST_USDT")
        assert margin == 48.0
        assert reserve == 12.0
        assert requested == 9_600.0
    finally:
        auto.BANK.balance_usdt = old_balance
        auto.CONTRACTS.clear()
        auto.CONTRACTS.update(old_contracts)


def test_auto_200x_profile_matches_old_10k_style_notional_when_supported():
    old_balance = auto.BANK.balance_usdt
    old_contracts = dict(auto.CONTRACTS)
    try:
        auto.BANK.balance_usdt = 100.0
        auto.CONTRACTS["TEST_USDT"] = type("C", (), {"max_leverage": 200})()
        requested, margin, _ = auto._requested_notional_and_margin("TEST_USDT")
        assert margin * 200.0 == 10_000.0
        assert requested == 10_000.0
    finally:
        auto.BANK.balance_usdt = old_balance
        auto.CONTRACTS.clear()
        auto.CONTRACTS.update(old_contracts)


def test_btw_uses_same_margin_reserve_rule():
    old_balance = btw.BANK.balance_usdt
    old_leverage = btw.EFFECTIVE_LEVERAGE
    try:
        btw.BANK.balance_usdt = 60.0
        btw.EFFECTIVE_LEVERAGE = 200.0
        requested, margin, reserve = btw._requested_notional_and_margin()
        assert margin == 48.0
        assert reserve == 12.0
        assert requested == 9_600.0
    finally:
        btw.BANK.balance_usdt = old_balance
        btw.EFFECTIVE_LEVERAGE = old_leverage
