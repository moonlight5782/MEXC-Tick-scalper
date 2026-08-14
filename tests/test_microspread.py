import math
from types import SimpleNamespace

from mexc_tick_scalper.demo_microspread_test import _required_edge
from mexc_tick_scalper.microspread import MicroSpreadModel


def px(base: float, bps: float) -> float:
    return base * math.exp(bps / 10_000.0)


def seed(model: MicroSpreadModel, *, until_ms: int = 3000, step_ms: int = 100) -> None:
    for ts in range(0, until_ms + 1, step_ms):
        model.update_binance(bid=99.99, ask=100.01, ts_ms=ts)
        model.update_mexc(bid=99.99, ask=100.01, ts_ms=ts)


def test_sub_one_bps_microspread_can_signal():
    model = MicroSpreadModel(
        horizon_ms=100,
        baseline_seconds=8,
        baseline_exclusion_ms=1000,
        min_edge_bps=0.35,
        min_binance_move_bps=0.02,
        max_binance_age_ms=300,
        max_mexc_age_ms=2000,
    )
    seed(model)

    moved = px(100.0, 0.60)
    model.update_binance(bid=moved - 0.01, ask=moved + 0.01, ts_ms=3100)
    snap = model.signal(now_ms=3100, threshold_bps=0.40)

    assert snap.ready is True
    assert snap.direction == 1
    assert 0.45 <= snap.edge_bps <= 0.75
    assert snap.binance_move_bps > 0.02


def test_same_excursion_fires_once_then_rearms_after_convergence():
    model = MicroSpreadModel(
        horizon_ms=100,
        baseline_seconds=8,
        baseline_exclusion_ms=1000,
        min_edge_bps=0.35,
        min_binance_move_bps=0.02,
    )
    seed(model)

    moved = px(100.0, 0.70)
    model.update_binance(bid=moved - 0.01, ask=moved + 0.01, ts_ms=3100)
    first = model.signal(now_ms=3100, threshold_bps=0.40)
    second = model.signal(now_ms=3110, threshold_bps=0.40)
    assert first.ready is True
    assert second.ready is False
    assert second.reason == "microspread_not_rearmed"

    model.update_mexc(bid=moved - 0.01, ask=moved + 0.01, ts_ms=3200)
    converged = model.signal(now_ms=3200, threshold_bps=0.40)
    assert converged.ready is False
    assert abs(converged.edge_bps) < 0.20

    moved2 = px(moved, 0.70)
    model.update_binance(bid=moved2 - 0.01, ask=moved2 + 0.01, ts_ms=3300)
    third = model.signal(now_ms=3300, threshold_bps=0.40)
    assert third.ready is True
    assert third.direction == 1


def test_mexc_quote_can_be_older_than_250ms_while_binance_must_be_fresh():
    model = MicroSpreadModel(
        horizon_ms=100,
        baseline_seconds=8,
        baseline_exclusion_ms=1000,
        min_edge_bps=0.35,
        min_binance_move_bps=0.02,
        max_binance_age_ms=300,
        max_mexc_age_ms=2000,
    )
    seed(model, until_ms=3000)

    moved = px(100.0, 0.65)
    model.update_binance(bid=moved - 0.01, ask=moved + 0.01, ts_ms=3800)
    snap = model.snapshot(now_ms=3800, threshold_bps=0.40)
    assert snap.mexc_age_ms == 800
    assert snap.binance_age_ms == 0
    assert snap.reason != "stale_mexc"

    stale = model.snapshot(now_ms=4200, threshold_bps=0.40)
    assert stale.reason == "stale_binance"


def test_live_spread_sets_executable_micro_threshold():
    assert math.isclose(
        _required_edge(spread_bps=0.20, min_edge_bps=0.35, min_net_edge_bps=0.20, spread_ratio=1.05),
        0.40,
    )
    assert math.isclose(
        _required_edge(spread_bps=1.00, min_edge_bps=0.35, min_net_edge_bps=0.20, spread_ratio=1.05),
        1.20,
    )


def test_microspread_runner_imports():
    import mexc_tick_scalper.demo_microspread_test as runner

    parser = runner.build_parser()
    args = parser.parse_args([])
    assert args.min_edge_bps == 0.35
    assert args.min_binance_move_bps == 0.02
    assert args.max_hold_seconds == 15.0
