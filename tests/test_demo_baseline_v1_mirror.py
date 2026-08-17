from mexc_tick_scalper.baseline_v1 import BASELINE_V1, apply_baseline_v1
from mexc_tick_scalper.demo_baseline_v1_mirror import ENTRY_RE, EXIT_RE, build_parser as mirror_parser
from mexc_tick_scalper.demo_baseline_v1_signal_test import build_parser as signal_parser


def test_entry_parser_matches_frozen_runner_output():
    line = (
        "ENTRY BTW_USDT SHORT requested=$10000 filled=$698 (7.0%) "
        "spread=2.79bps slip=0.00bps residual=-11.18bps cost=4.82bps"
    )
    match = ENTRY_RE.search(line)
    assert match is not None
    assert match.group("symbol") == "BTW_USDT"
    assert match.group("side") == "SHORT"
    assert float(match.group("filled")) == 698.0


def test_exit_parser_matches_frozen_runner_output():
    line = "EXIT BANK_USDT mexc_catchup_convergence pnl +6.28bps +$0.44 hold 152ms"
    match = EXIT_RE.search(line)
    assert match is not None
    assert match.group("symbol") == "BANK_USDT"
    assert match.group("reason") == "mexc_catchup_convergence"


def test_demo_mirror_does_not_require_a_zero_fee_symbol_argument():
    args = mirror_parser().parse_args([])
    assert args.demo_symbol == ""
    assert args.demo_ioc_cross_bps == 1.0


def test_demo_signal_runner_reuses_frozen_baseline_thresholds():
    args = signal_parser().parse_args(["--demo-test-symbol", "BTC_USDT"])
    apply_baseline_v1(args)
    assert args.min_absolute_residual_bps == BASELINE_V1["min_absolute_residual_bps"] == 8.0
    assert args.min_signal_strength_ratio == BASELINE_V1["min_signal_strength_ratio"] == 3.0
    assert args.min_residual_retention == BASELINE_V1["min_residual_retention"] == 0.60
    assert args.min_impulse_retention == BASELINE_V1["min_impulse_retention"] == 0.75
    assert args.ioc_cross_bps == BASELINE_V1["ioc_cross_bps"] == 1.0
    assert args.max_entry_slippage_bps == BASELINE_V1["max_entry_slippage_bps"] == 1.0
    assert args.min_executable_net_edge_bps == BASELINE_V1["min_executable_net_edge_bps"] == 2.0
    assert args.min_edge_to_cost_ratio == BASELINE_V1["min_edge_to_cost_ratio"] == 1.50
    assert args.mid_adverse_cut_bps == BASELINE_V1["mid_adverse_cut_bps"] == 3.0
    assert args.trailing_distance_bps == BASELINE_V1["trailing_distance_bps"] == 1.5
