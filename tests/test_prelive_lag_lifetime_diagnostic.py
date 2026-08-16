from __future__ import annotations

import inspect

from mexc_tick_scalper.prelive_lag_lifetime_diagnostic import (
    SignalTracker,
    _summary,
    _terminal_reason,
)
import mexc_tick_scalper.prelive_lag_lifetime_diagnostic as diagnostic


def tracker(**overrides):
    values = dict(
        signal_id="s1",
        symbol="TEST_USDT",
        direction=1,
        signal_ms=1_000,
        signal_monotonic=1.0,
        signal_residual_bps=5.0,
        signal_threshold_bps=1.0,
        signal_spread_bps=0.5,
        signal_noise_bps=0.2,
        signal_leader_advantage_bps=3.0,
        measured_rtt_ms=300.0,
    )
    values.update(overrides)
    return SignalTracker(**values)


def test_convergence_ends_lag_lifetime():
    t = tracker()
    reason = _terminal_reason(
        t,
        residual_bps=0.5,
        convergence_bps=0.1,
        convergence_fraction=0.2,
        reversal_edge_bps=0.35,
        age_ms=150.0,
        max_track_ms=5_000.0,
    )
    assert reason == "convergence"


def test_same_direction_residual_does_not_end_before_timeout():
    t = tracker()
    reason = _terminal_reason(
        t,
        residual_bps=3.0,
        convergence_bps=0.1,
        convergence_fraction=0.2,
        reversal_edge_bps=0.35,
        age_ms=299.0,
        max_track_ms=5_000.0,
    )
    assert reason is None


def test_residual_reversal_ends_lifetime():
    t = tracker()
    reason = _terminal_reason(
        t,
        residual_bps=-0.8,
        convergence_bps=0.1,
        convergence_fraction=0.2,
        reversal_edge_bps=0.35,
        age_ms=100.0,
        max_track_ms=5_000.0,
    )
    assert reason == "residual_reversal"


def test_timeout_is_only_terminal_when_lag_has_not_converged_or_reversed():
    t = tracker()
    reason = _terminal_reason(
        t,
        residual_bps=2.0,
        convergence_bps=0.1,
        convergence_fraction=0.2,
        reversal_edge_bps=0.35,
        age_ms=5_000.0,
        max_track_ms=5_000.0,
    )
    assert reason == "track_timeout"


def test_summary_counts_each_signal_independently():
    a = tracker(signal_id="a", terminal_ms=1_200, terminal_reason="convergence", rtt_checked=False)
    b = tracker(
        signal_id="b", terminal_ms=1_400, terminal_reason="convergence",
        rtt_checked=True, rtt_survived=True,
    )
    c = tracker(
        signal_id="c", terminal_ms=1_350, terminal_reason="residual_reversal",
        rtt_checked=True, rtt_survived=False,
    )
    text = _summary([a, b, c], measured_rtt_ms=300.0)
    assert "signals=3" in text
    assert "rtt_checked=2" in text
    assert "survived@RTT=1/2" in text
    assert "reasons conv/reversal/timeout=2/1/0" in text


def test_diagnostic_has_no_live_write_path():
    source = inspect.getsource(diagnostic)
    assert "write_enabled=True" not in source
    assert "open_ioc(" not in source
    assert "close_market_reduce_only(" not in source
    assert "close_position_snapshot_reduce_only(" not in source
