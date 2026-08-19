from types import SimpleNamespace

import mexc_tick_scalper.auto_discovery_shadow as auto
import mexc_tick_scalper.btw_economic_arrival_shadow as btw


def test_auto_targets_10k_notional_at_200x_without_target_margin():
    old_contracts = dict(auto.CONTRACTS)
    old_balance = auto.BANK.balance_usdt
    try:
        auto.CONTRACTS.clear()
        auto.CONTRACTS["AAA_USDT"] = SimpleNamespace(max_leverage=200)
        auto.BANK.balance_usdt = 100.0
        requested, margin, reserve = auto._requested_notional_and_margin("AAA_USDT")
        assert requested == 10_000.0
        assert margin == 50.0
        assert reserve == 50.0
    finally:
        auto.CONTRACTS.clear()
        auto.CONTRACTS.update(old_contracts)
        auto.BANK.balance_usdt = old_balance


def test_auto_scales_notional_down_only_to_preserve_equity_reserve():
    old_contracts = dict(auto.CONTRACTS)
    old_balance = auto.BANK.balance_usdt
    try:
        auto.CONTRACTS.clear()
        auto.CONTRACTS["AAA_USDT"] = SimpleNamespace(max_leverage=200)
        auto.BANK.balance_usdt = 60.0
        requested, margin, reserve = auto._requested_notional_and_margin("AAA_USDT")
        assert requested == 9_600.0
        assert margin == 48.0
        assert reserve == 12.0
    finally:
        auto.CONTRACTS.clear()
        auto.CONTRACTS.update(old_contracts)
        auto.BANK.balance_usdt = old_balance


def test_lower_live_leverage_reduces_notional_without_inventing_margin_target():
    old_contracts = dict(auto.CONTRACTS)
    old_balance = auto.BANK.balance_usdt
    try:
        auto.CONTRACTS.clear()
        auto.CONTRACTS["LOW_USDT"] = SimpleNamespace(max_leverage=20)
        auto.BANK.balance_usdt = 100.0
        requested, margin, reserve = auto._requested_notional_and_margin("LOW_USDT")
        assert requested == 1_600.0
        assert margin == 80.0
        assert reserve == 20.0
    finally:
        auto.CONTRACTS.clear()
        auto.CONTRACTS.update(old_contracts)
        auto.BANK.balance_usdt = old_balance


def test_btw_uses_same_historical_notional_rule():
    old_lev = btw.EFFECTIVE_LEVERAGE
    old_balance = btw.BANK.balance_usdt
    try:
        btw.EFFECTIVE_LEVERAGE = 200.0
        btw.BANK.balance_usdt = 100.0
        requested, margin, reserve = btw._requested_notional_and_margin()
        assert requested == 10_000.0
        assert margin == 50.0
        assert reserve == 50.0
    finally:
        btw.EFFECTIVE_LEVERAGE = old_lev
        btw.BANK.balance_usdt = old_balance
