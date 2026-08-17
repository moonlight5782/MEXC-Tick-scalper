from types import SimpleNamespace

from mexc_tick_scalper.demo_execution_validation_v1 import (
    EmergencyExecutableExitPolicy,
    build_parser,
)
from mexc_tick_scalper.hybrid_strategy import MicrostructureSnapshot


def _snap(direction: int = 1, confidence: float = 0.8):
    return MicrostructureSnapshot(
        direction=direction,
        momentum_bps=0.0,
        cvd_norm=0.0,
        buy_ratio=0.5,
        confidence=confidence,
        trade_rate=10.0,
        price_changes=10,
    )


def test_demo_execution_defaults_use_agreed_margin_and_max_leverage():
    args = build_parser().parse_args(["--symbol", "BTC_USDT"])
    assert args.target_margin_usdt == 60.0
    assert args.leverage == 0
    assert args.emergency_executable_cut_bps == 3.0


def test_executable_price_emergency_cut_is_independent_of_flow():
    policy = EmergencyExecutableExitPolicy(
        side=1,
        entry_price=100.0,
        winner_arm_bps=0.5,
        winner_pullback_bps=1.5,
        min_hold_seconds=0.05,
    )
    policy.emergency_executable_cut_bps = 3.0
    reason = policy.on_tick(
        price=99.96,
        liquidation_price=None,
        signal=_snap(direction=1, confidence=1.0),
        age_seconds=0.01,
        signal_fresh=True,
    )
    assert reason == "emergency_executable_cut"


def test_staged_trailing_from_existing_policy_remains_active():
    policy = EmergencyExecutableExitPolicy(
        side=1,
        entry_price=100.0,
        winner_arm_bps=0.5,
        winner_pullback_bps=1.5,
        min_hold_seconds=0.05,
    )
    policy.emergency_executable_cut_bps = 3.0
    assert policy.on_tick(
        price=100.06,
        liquidation_price=None,
        signal=_snap(),
        age_seconds=1.0,
        signal_fresh=True,
    ) is None
    assert policy.trailing_stop_bps is not None
    assert policy.on_tick(
        price=100.04,
        liquidation_price=None,
        signal=_snap(),
        age_seconds=1.1,
        signal_fresh=True,
    ) == "winner_staged_trailing_stop"
