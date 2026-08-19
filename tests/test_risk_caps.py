import mexc_tick_scalper.auto_discovery_shadow as auto
import mexc_tick_scalper.btw_economic_arrival_shadow as btw


def test_auto_margin_cap_is_ten_percent_of_equity():
    bank = auto.BankState(balance_usdt=100.0)
    assert bank.max_isolated_margin_usdt == 10.0
    bank.balance_usdt = 90.0
    assert bank.max_isolated_margin_usdt == 9.0


def test_auto_session_kill_switch_threshold_is_twenty_percent_drawdown():
    bank = auto.BankState(balance_usdt=100.0)
    assert bank.drawdown_stop_balance == 80.0


def test_btw_buying_power_uses_margin_cap_not_full_bank():
    old = btw.EFFECTIVE_LEVERAGE
    try:
        btw.EFFECTIVE_LEVERAGE = 20.0
        bank = btw.BankState(balance_usdt=100.0)
        assert bank.max_isolated_margin_usdt == 10.0
        assert bank.buying_power_usdt == 200.0
    finally:
        btw.EFFECTIVE_LEVERAGE = old
