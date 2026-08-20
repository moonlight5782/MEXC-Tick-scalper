from __future__ import annotations

import pytest

from mexc_tick_scalper.liquidation_guard import (
    build_isolated_liquidation_state,
    estimated_mmr,
    estimated_risk_level,
    fair_distance_to_liquidation_bps,
    fair_price_crossed_liquidation,
)
from mexc_tick_scalper.live_zero_fee_universe import LiveZeroFeeContract


def _contract(**overrides) -> LiveZeroFeeContract:
    values = dict(
        mexc_symbol="TEST_USDT",
        binance_symbol="TESTUSDT",
        max_leverage=125,
        contract_size=0.0001,
        min_vol=1.0,
        maintenance_margin_rate=0.005,
        initial_margin_rate=0.04,
        risk_base_vol=150_000,
        risk_incr_vol=150_000,
        risk_incr_mmr=0.005,
        risk_level_limit=5,
        risk_limit_type="BY_VOLUME",
    )
    values.update(overrides)
    return LiveZeroFeeContract(**values)


def test_official_mexc_long_liquidation_example() -> None:
    # MEXC's published isolated example: 10,000 contracts * 0.0001 BTC = 1 BTC,
    # entry 8000, leverage 25x, MMR 0.5% -> liquidation price 7720.
    state = build_isolated_liquidation_state(
        contract=_contract(),
        symbol="TEST_USDT",
        direction=1,
        entry_price=8_000.0,
        qty_base=1.0,
        leverage=25.0,
    )
    assert state is not None
    assert state.position_margin_usdt == pytest.approx(320.0)
    assert state.maintenance_margin_usdt == pytest.approx(40.0)
    assert state.liquidation_price == pytest.approx(7_720.0)
    assert state.liquidation_distance_bps == pytest.approx(350.0)


def test_short_liquidation_is_symmetric_without_liquidation_fee() -> None:
    state = build_isolated_liquidation_state(
        contract=_contract(),
        symbol="TEST_USDT",
        direction=-1,
        entry_price=8_000.0,
        qty_base=1.0,
        leverage=25.0,
    )
    assert state is not None
    assert state.liquidation_price == pytest.approx(8_280.0)
    assert state.liquidation_distance_bps == pytest.approx(350.0)


def test_public_risk_tier_increases_mmr_conservatively() -> None:
    contract = _contract(
        contract_size=1.0,
        risk_base_vol=100.0,
        risk_incr_vol=100.0,
        risk_incr_mmr=0.001,
        maintenance_margin_rate=0.005,
    )
    assert estimated_risk_level(contract, qty_base=99.0, notional_usdt=99.0) == 1
    assert estimated_risk_level(contract, qty_base=101.0, notional_usdt=101.0) == 2
    assert estimated_risk_level(contract, qty_base=201.0, notional_usdt=201.0) == 3
    mmr, level = estimated_mmr(contract, qty_base=201.0, notional_usdt=201.0)
    assert level == 3
    assert mmr == pytest.approx(0.007)


def test_live_fair_price_crossing_detects_long_liquidation() -> None:
    state = build_isolated_liquidation_state(
        contract=_contract(), symbol="TEST_USDT", direction=1,
        entry_price=8_000.0, qty_base=1.0, leverage=25.0,
    )
    assert state is not None
    assert fair_price_crossed_liquidation(state, 7_721.0) is False
    assert fair_price_crossed_liquidation(state, 7_720.0) is True
    assert fair_price_crossed_liquidation(state, 7_700.0) is True
    assert fair_distance_to_liquidation_bps(state, 8_000.0) > 0


def test_live_fair_price_crossing_detects_short_liquidation() -> None:
    state = build_isolated_liquidation_state(
        contract=_contract(), symbol="TEST_USDT", direction=-1,
        entry_price=8_000.0, qty_base=1.0, leverage=25.0,
    )
    assert state is not None
    assert fair_price_crossed_liquidation(state, 8_279.0) is False
    assert fair_price_crossed_liquidation(state, 8_280.0) is True
    assert fair_price_crossed_liquidation(state, 8_300.0) is True
