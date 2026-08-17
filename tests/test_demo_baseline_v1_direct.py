from mexc_tick_scalper.baseline_v1 import BASELINE_V1, apply_baseline_v1
from mexc_tick_scalper.demo_baseline_v1_direct import _cross_limit, build_parser
from mexc_tick_scalper.execution import OrderSide


def test_direct_runner_uses_frozen_baseline_values():
    args = build_parser().parse_args([])
    apply_baseline_v1(args)
    assert args.target_notional_usdt == BASELINE_V1["target_notional_usdt"] == 10000.0
    assert args.min_absolute_residual_bps == BASELINE_V1["min_absolute_residual_bps"] == 8.0
    assert args.min_signal_strength_ratio == BASELINE_V1["min_signal_strength_ratio"] == 3.0
    assert args.ioc_cross_bps == BASELINE_V1["ioc_cross_bps"] == 1.0
    assert args.trailing_distance_bps == BASELINE_V1["trailing_distance_bps"] == 1.5


def test_cross_limit_rounds_long_up_to_price_unit():
    assert _cross_limit(100.0, OrderSide.LONG, 1.0, 0.1) == 100.1


def test_cross_limit_rounds_short_down_to_price_unit():
    assert _cross_limit(100.0, OrderSide.SHORT, 1.0, 0.1) == 99.9
