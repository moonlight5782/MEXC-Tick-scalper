from pathlib import Path

from mexc_tick_scalper.execution import OrderSide
from mexc_tick_scalper.testnet_known_good_risk import (
    adverse_roe_pct,
    basis_bps,
    build_parser,
    liquidation_distance_bps,
    theoretical_isolated_liq_price,
)


def test_default_execution_risk_is_not_exchange_max_leverage():
    args = build_parser().parse_args([])
    assert args.risk_max_leverage == 10
    assert args.max_adverse_roe_pct == 8.0
    assert args.min_liq_distance_bps == 500.0
    assert args.emergency_liq_distance_bps == 300.0


def test_long_and_short_liquidation_distance_are_directional():
    assert round(liquidation_distance_bps(OrderSide.LONG, 100.0, 95.0), 6) == 500.0
    assert round(liquidation_distance_bps(OrderSide.SHORT, 100.0, 105.0), 6) == 500.0


def test_theoretical_liquidation_uses_isolated_margin_formula():
    long_liq = theoretical_isolated_liq_price(OrderSide.LONG, 100.0, 10, 0.005, 0.0)
    short_liq = theoretical_isolated_liq_price(OrderSide.SHORT, 100.0, 10, 0.005, 0.0)
    assert round(long_liq, 4) == 90.5
    assert round(short_liq, 4) == 109.5


def test_adverse_roe_guard_scales_with_leverage():
    assert round(adverse_roe_pct(OrderSide.LONG, 100.0, 99.0, 10), 6) == -10.0
    assert round(adverse_roe_pct(OrderSide.SHORT, 100.0, 101.0, 10), 6) == -10.0


def test_testnet_basis_guard_is_symmetric():
    assert round(basis_bps(100.0, 100.5), 6) == 50.0
    assert round(basis_bps(100.0, 99.5), 6) == 50.0


def test_runner_structurally_has_single_ioc_submit_and_no_topup_loop():
    source = Path(__file__).parents[1] / "src" / "mexc_tick_scalper" / "testnet_known_good_risk.py"
    text = source.read_text(encoding="utf-8")
    # One entry IOC submission site. The remainder is never chased or topped up.
    assert text.count("adapter.open_ioc(") == 1
    assert "top_up" not in text
    assert "topup" not in text
    assert "retry_entry" not in text
    assert "target_qty = args.target_notional_usdt / testnet_best" in text
    assert "actual_notional = remote.qty * actual_entry" in text
