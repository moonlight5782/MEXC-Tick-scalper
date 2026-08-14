from types import SimpleNamespace

from mexc_tick_scalper.demo_multi_lead_lag_test import (
    _best_candidate,
    _candidate_from_snapshot,
    _required_live_edge,
)
from mexc_tick_scalper.lead_lag import LeadLagSnapshot
from mexc_tick_scalper.live_lead_lag_shadow import BestBook


def _args():
    return SimpleNamespace(
        min_edge_bps=4.0,
        min_net_edge_bps=2.0,
        edge_to_spread_ratio=1.15,
    )


def _snap(edge: float, direction: int = 1) -> LeadLagSnapshot:
    return LeadLagSnapshot(
        ready=True,
        direction=direction,
        edge_bps=edge,
        raw_gap_bps=edge,
        baseline_gap_bps=0.0,
        binance_move_bps=8.0 * direction,
        mexc_move_bps=1.0 * direction,
        binance_mid=100.0,
        mexc_mid=99.9,
        age_ms=20.0,
        reason="lead_lag_confirmed",
    )


def test_required_live_edge_covers_spread_and_net_buffer():
    assert _required_live_edge(
        min_edge_bps=4.0,
        live_spread_bps=1.0,
        min_net_edge_bps=2.0,
        edge_to_spread_ratio=1.15,
    ) == 4.0
    assert _required_live_edge(
        min_edge_bps=4.0,
        live_spread_bps=5.0,
        min_net_edge_bps=2.0,
        edge_to_spread_ratio=1.15,
    ) == 7.0


def test_candidate_rejects_raw_gap_that_does_not_clear_live_spread():
    book = BestBook(bid=99.95, ask=100.05, ts_ms=1)  # about 10 bps spread
    candidate = _candidate_from_snapshot("TEST_USDT", _snap(10.5), book, _args())
    assert candidate is None


def test_best_candidate_prefers_larger_executable_margin_not_raw_edge():
    args = _args()
    tight = BestBook(bid=99.995, ask=100.005, ts_ms=1)  # about 1 bps
    wide = BestBook(bid=99.95, ask=100.05, ts_ms=1)     # about 10 bps

    a = _candidate_from_snapshot("TIGHT_USDT", _snap(9.0), tight, args)
    b = _candidate_from_snapshot("WIDE_USDT", _snap(13.0), wide, args)

    assert a is not None
    assert b is not None
    chosen = _best_candidate([a, b])
    assert chosen is not None
    assert chosen.symbol == "TIGHT_USDT"


def test_short_direction_is_preserved():
    book = BestBook(bid=99.995, ask=100.005, ts_ms=1)
    candidate = _candidate_from_snapshot("SHORT_USDT", _snap(-9.0, direction=-1), book, _args())
    assert candidate is not None
    assert candidate.direction == -1
    assert candidate.edge_bps < 0
