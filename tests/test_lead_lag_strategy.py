from types import SimpleNamespace

import pytest

from mexc_tick_scalper.lead_lag_strategy import (
    LeadLagGate,
    convergence_threshold,
    spread_aware_adverse_cut,
)


def snap(*, edge=2.0, b=1.5, m=0.2, reason="microspread_confirmed"):
    return SimpleNamespace(
        edge_bps=edge,
        binance_move_bps=b,
        mexc_move_bps=m,
        binance_age_ms=0.0,
        mexc_age_ms=0.0,
        reason=reason,
    )


def test_rejects_small_binance_noise_even_if_residual_crossed_floor():
    gate = LeadLagGate(min_binance_move_bps=0.5, confirm_updates=1, confirm_ms=0)
    d = gate.observe("X", snap(edge=1.0, b=0.2, m=0.0), 0.2, 1000, event_key=(1, 1))
    assert not d.ready
    assert d.reason == "binance_move_is_noise"


def test_rejects_when_mexc_already_followed_binance():
    gate = LeadLagGate(min_binance_move_bps=0.5, min_leader_advantage_bps=0.25, confirm_updates=1, confirm_ms=0)
    d = gate.observe("X", snap(edge=2.0, b=1.2, m=1.1), 0.2, 1000, event_key=(1, 1))
    assert not d.ready
    assert d.reason == "mexc_not_lagging_enough"


def test_requires_two_independent_updates_before_entry():
    gate = LeadLagGate(confirm_updates=2, confirm_ms=10, min_binance_move_bps=0.5)
    first = gate.observe("X", snap(edge=2.0, b=1.5, m=0.2), 0.2, 1000, event_key=(1, 1))
    assert not first.ready
    assert first.reason == "lag_confirming"
    duplicate = gate.observe("X", snap(edge=2.0, b=1.5, m=0.2), 0.2, 1015, event_key=(1, 1))
    assert not duplicate.ready
    assert duplicate.reason == "duplicate_market_state"
    second = gate.observe("X", snap(edge=2.0, b=1.5, m=0.2), 0.2, 1015, event_key=(2, 1))
    assert second.ready


def test_adaptive_noise_raises_required_residual():
    gate = LeadLagGate(residual_noise_multiplier=3.0, confirm_updates=1, confirm_ms=0)
    state = gate._s("X")
    # Alternating +/- 1 bps residual is noisy; threshold should exceed the static 0.5 bps floor.
    for i in range(20):
        state.residuals.append((i * 100, 1.0 if i % 2 else -1.0))
    d = gate.assess("X", snap(edge=1.0, b=2.0, m=0.0), 0.1, 2000)
    assert d.noise_bps > 0.5
    assert d.threshold_bps > 1.0
    assert not d.ready


def test_spread_aware_adverse_cut_does_not_treat_bid_ask_as_loss_signal():
    assert spread_aware_adverse_cut(6.0, 1.5, 1.25) == pytest.approx(7.5)
    assert spread_aware_adverse_cut(0.2, 1.5, 1.25) == pytest.approx(1.5)


def test_convergence_threshold_scales_with_entry_residual():
    assert convergence_threshold(4.0, 0.10, 0.20) == pytest.approx(0.8)
    assert convergence_threshold(0.2, 0.10, 0.20) == pytest.approx(0.10)
