from mexc_tick_scalper.execution import OrderSide
from mexc_tick_scalper.testnet_known_good_v1 import KNOWN_GOOD_COMMIT, _cross_limit, build_parser


def test_runner_points_to_proven_commit():
    assert KNOWN_GOOD_COMMIT == "372c3b286eb82aa4b87d806999f8db47173a2b3e"


def test_runner_keeps_known_good_strategy_defaults():
    args = build_parser().parse_args([])
    assert args.target_notional_usdt == 10000.0
    assert args.min_absolute_residual_bps == 8.0
    assert args.min_signal_strength_ratio == 3.0
    assert args.min_residual_retention == 0.60
    assert args.min_impulse_retention == 0.75
    assert args.ioc_cross_bps == 1.0
    assert args.max_entry_slippage_bps == 1.0
    assert args.min_executable_net_edge_bps == 2.0
    assert args.min_edge_to_cost_ratio == 1.50
    assert args.trailing_distance_bps == 1.5


def test_testnet_limit_price_rounds_outward_to_contract_tick():
    detail = {"priceUnit": "0.1"}
    assert _cross_limit(100.0, OrderSide.LONG, 1.0, detail) == 100.1
    assert _cross_limit(100.0, OrderSide.SHORT, 1.0, detail) == 99.9


def test_default_stop_is_exact_100_real_closed_trades():
    assert build_parser().parse_args([]).target_closed_trades == 100
