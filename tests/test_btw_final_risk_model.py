from types import SimpleNamespace

import mexc_tick_scalper.btw_economic_arrival_shadow as btw


def _signal(direction=1, residual=50.0):
    return SimpleNamespace(
        direction=direction,
        residual_bps=residual,
        binance_price=100.0,
        binance_move_bps=10.0,
    )


def test_bank_request_uses_effective_leverage_and_reserve_cap():
    old_leverage = btw.EFFECTIVE_LEVERAGE
    old_balance = btw.BANK.balance_usdt
    try:
        btw.EFFECTIVE_LEVERAGE = 20.0
        btw.BANK.balance_usdt = 100.0
        requested, margin, reserve = btw._requested_notional_and_margin()
        assert requested == 1600.0
        assert margin == 80.0
        assert reserve == 20.0

        btw.BANK.balance_usdt = 125.0
        requested, margin, reserve = btw._requested_notional_and_margin()
        assert requested == 2000.0
        assert margin == 100.0
        assert reserve == 25.0
    finally:
        btw.EFFECTIVE_LEVERAGE = old_leverage
        btw.BANK.balance_usdt = old_balance


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
