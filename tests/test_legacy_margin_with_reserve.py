import mexc_tick_scalper.auto_discovery_shadow as auto
import mexc_tick_scalper.btw_economic_arrival_shadow as btw


def test_auto_legacy_margin_targets_50_usdt_at_100_bank():
    bank = auto.BankState(balance_usdt=100.0)
    assert bank.isolated_margin_usdt == 50.0
    assert bank.reserve_usdt == 50.0


def test_auto_legacy_margin_never_uses_entire_smaller_bank():
    bank = auto.BankState(balance_usdt=60.0)
    assert bank.isolated_margin_usdt == 48.0
    assert bank.reserve_usdt == 12.0


def test_auto_200x_profile_matches_old_10k_style_notional_when_supported():
    bank = auto.BankState(balance_usdt=100.0)
    assert bank.isolated_margin_usdt * 200.0 == 10_000.0


def test_btw_uses_same_margin_reserve_rule():
    bank = btw.BankState(balance_usdt=60.0)
    assert bank.isolated_margin_usdt == 48.0
    assert bank.reserve_usdt == 12.0
