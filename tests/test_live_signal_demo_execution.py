from mexc_tick_scalper.demo_lead_lag_test import (
    _fee_cache_allows_entry,
    _required_live_edge,
)
from mexc_tick_scalper.demo_live_launcher import _build_microspread_command, _candidate_score
from mexc_tick_scalper.models import FeeStatus
from mexc_tick_scalper.state import EligibilityState, apply_fee_status


def test_required_edge_covers_live_spread_and_net_profit_buffer():
    required = _required_live_edge(
        min_edge_bps=4.0,
        live_spread_bps=5.0,
        min_net_edge_bps=2.0,
        edge_to_spread_ratio=1.15,
    )
    assert required == 7.0


def test_required_edge_keeps_absolute_floor_when_live_spread_is_tiny():
    required = _required_live_edge(
        min_edge_bps=4.0,
        live_spread_bps=0.8,
        min_net_edge_bps=2.0,
        edge_to_spread_ratio=1.15,
    )
    assert required == 4.0


def test_live_zero_fee_cache_must_be_fresh_and_exactly_zero():
    state = EligibilityState("VELVET_USDT")
    zero = FeeStatus(0.0, 0.0, "live")
    apply_fee_status(state, zero, 1_000)

    assert _fee_cache_allows_entry(
        zero,
        state,
        checked_at_ms=1_000,
        now_ms=5_000,
        max_age_ms=8_000,
    )
    assert not _fee_cache_allows_entry(
        zero,
        state,
        checked_at_ms=1_000,
        now_ms=10_000,
        max_age_ms=8_000,
    )

    nonzero = FeeStatus(0.0, 0.0001, "live")
    apply_fee_status(state, nonzero, 11_000)
    assert not _fee_cache_allows_entry(
        nonzero,
        state,
        checked_at_ms=11_000,
        now_ms=11_100,
        max_age_ms=8_000,
    )


def test_candidate_score_rewards_repeatable_edge_after_spread():
    velvet_like = _candidate_score(events=11, avg_edge_bps=13.88, live_spread_bps=1.07)
    beat_like = _candidate_score(events=40, avg_edge_bps=15.62, live_spread_bps=15.05)
    assert velvet_like > beat_like


def test_launcher_uses_xaut_demo_aligned_zero_fee_profile():
    cmd = _build_microspread_command(
        "python", seconds=21600, cycles=100, leverage=1000, margin=0.1,
    )

    assert cmd[0] == "python"
    assert cmd[cmd.index("--include-symbols") + 1] == "XAUT_USDT"
    assert "--demo-zero-fee-only" in cmd
    assert cmd[cmd.index("--signal-mexc-source") + 1] == "demo"
    assert cmd[cmd.index("--min-edge-bps") + 1] == "0.70"
    assert cmd[cmd.index("--min-net-edge-bps") + 1] == "0.60"
    assert cmd[cmd.index("--demo-ioc-cross-bps") + 1] == "1"
    assert "--allow-demo-fee-accounting" not in cmd
    assert "--max-demo-volume" not in cmd
