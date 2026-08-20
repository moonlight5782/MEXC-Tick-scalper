import mexc_tick_scalper.auto_discovery_shadow as auto
import mexc_tick_scalper.btw_economic_arrival_shadow as btw


def test_auto_margin_cap_reserves_twenty_percent_of_equity():
    bank = auto.BankState(balance_usdt=100.0)
    assert bank.max_allocatable_margin_usdt == 80.0
    bank.balance_usdt = 90.0
    assert bank.max_allocatable_margin_usdt == 72.0


def test_auto_session_kill_switch_threshold_is_sixty_percent_drawdown():
    bank = auto.BankState(balance_usdt=100.0)
    assert bank.drawdown_stop_balance == 40.0


def test_btw_requested_notional_uses_margin_cap_not_full_bank():
    old_leverage = btw.EFFECTIVE_LEVERAGE
    old_balance = btw.BANK.balance_usdt
    try:
        btw.EFFECTIVE_LEVERAGE = 20.0
        btw.BANK.balance_usdt = 100.0
        assert btw.BANK.max_allocatable_margin_usdt == 80.0
        requested, margin, reserve = btw._requested_notional_and_margin()
        assert margin == 80.0
        assert reserve == 20.0
        assert requested == 1600.0
    finally:
        btw.EFFECTIVE_LEVERAGE = old_leverage
        btw.BANK.balance_usdt = old_balance
