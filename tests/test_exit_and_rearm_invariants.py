from __future__ import annotations

from types import SimpleNamespace

from mexc_tick_scalper.lead_lag_strategy import LeadLagGate
from mexc_tick_scalper.microspread import MicroSpreadSnapshot


def _snap(*, edge: float, binance_move: float = 12.0, mexc_move: float = 1.0) -> MicroSpreadSnapshot:
    direction = 1 if edge > 0 else -1 if edge < 0 else 0
    return MicroSpreadSnapshot(
        ready=True,
        direction=direction,
        edge_bps=edge,
        raw_gap_bps=edge,
        baseline_gap_bps=0.0,
        binance_move_bps=binance_move if direction >= 0 else -abs(binance_move),
        mexc_move_bps=mexc_move if direction >= 0 else -abs(mexc_move),
        binance_mid=100.0,
        mexc_mid=100.0,
        age_ms=0.0,
        binance_age_ms=0.0,
        mexc_age_ms=0.0,
        threshold_bps=0.0,
        reason="test",
    )


def test_same_pair_can_trade_again_after_rearm() -> None:
    gate = LeadLagGate(
        noise_window_ms=8_000,
        residual_noise_multiplier=0.0,
        binance_noise_multiplier=0.0,
        min_edge_bps=2.0,
        min_net_edge_bps=0.0,
        spread_ratio=1.0,
        min_binance_move_bps=1.0,
        min_leader_advantage_bps=1.0,
        min_lead_ratio=1.0,
        confirm_updates=1,
        confirm_ms=0,
        rearm_fraction=0.35,
    )

    first = gate.observe("AAA_USDT", _snap(edge=10.0), 0.2, 1_000, event_key=(1, 1))
    assert first.ready is True

    duplicate_impulse = gate.observe("AAA_USDT", _snap(edge=9.0), 0.2, 1_001, event_key=(2, 2))
    assert duplicate_impulse.ready is False
    assert duplicate_impulse.reason == "lag_not_rearmed"

    reset = gate.observe(
        "AAA_USDT",
        _snap(edge=0.1, binance_move=0.0, mexc_move=0.0),
        0.2,
        1_002,
        event_key=(3, 3),
    )
    assert reset.ready is False

    second = gate.observe("AAA_USDT", _snap(edge=11.0), 0.2, 1_003, event_key=(4, 4))
    assert second.ready is True


def test_auto_shadow_configures_zero_artificial_hold_before_exit() -> None:
    import mexc_tick_scalper.auto_discovery_shadow as auto

    args = SimpleNamespace(
        min_hold_ms=999,
        mid_adverse_cut_bps=999.0,
        trailing_distance_bps=999.0,
    )
    auto._apply_immediate_exit_policy(args)

    assert args.min_hold_ms == 0
    assert args.mid_adverse_cut_bps == auto.EMERGENCY_ADVERSE_BPS
    assert args.trailing_distance_bps == 0.0
