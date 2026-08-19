from types import SimpleNamespace

import mexc_tick_scalper.btw_economic_arrival_shadow as btw


def _signal(direction=1, residual=50.0):
    return SimpleNamespace(
        direction=direction,
        residual_bps=residual,
        binance_price=100.0,
        binance_move_bps=10.0,
    )


def test_bank_buying_power_uses_effective_leverage():
    old = btw.EFFECTIVE_LEVERAGE
    try:
        btw.EFFECTIVE_LEVERAGE = 20.0
        bank = btw.BankState(balance_usdt=100.0)
        assert bank.buying_power_usdt == 2000.0
        bank.balance_usdt = 125.0
        assert bank.buying_power_usdt == 2500.0
    finally:
        btw.EFFECTIVE_LEVERAGE = old


def test_arrival_retention_is_diagnostic_only_when_absolute_edge_survives():
    ok, why, retention, _ = btw.economic_arrival_entry_ok(
        signal=_signal(residual=50.0),
        current_residual_bps=20.0,
        current_binance_price=100.0,
        current_spread_bps=3.0,
        min_residual_retention=0.60,
        min_impulse_retention=0.75,
        min_remaining_edge_bps=8.0,
        min_edge_after_spread_bps=2.0,
    )
    assert ok is True
    assert why == "absolute_edge_survived"
    assert retention == 0.4


def test_arrival_reversal_still_rejects():
    ok, why, _, _ = btw.economic_arrival_entry_ok(
        signal=_signal(direction=1, residual=50.0),
        current_residual_bps=-5.0,
        current_binance_price=100.0,
        current_spread_bps=1.0,
        min_residual_retention=0.60,
        min_impulse_retention=0.75,
        min_remaining_edge_bps=8.0,
        min_edge_after_spread_bps=2.0,
    )
    assert ok is False
    assert why == "residual_reversed"
