from mexc_tick_scalper.models import Tick
from mexc_tick_scalper.shadow import replay, best_result


def mk(prices):
    return [Tick("TEST_USDT", float(p), 1.0, 1, i * 1000) for i, p in enumerate(prices)]


def test_replay_detects_momentum_and_exit():
    ticks = mk([100, 101, 102, 103, 104, 103, 102, 101, 100, 99, 100])
    result = replay("TEST_USDT", ticks, momentum_ticks=3, reversal_ticks=1)
    assert result.trades >= 1


def test_best_result_returns_candidate_when_enough_trades():
    prices = [100, 101, 102, 103, 104, 103, 102, 101, 100, 99, 100] * 8
    result = best_result(
        "TEST_USDT",
        mk(prices),
        momentum_grid=[2, 3],
        reversal_grid=[1],
        max_hold_seconds=30,
        min_trades=2,
    )
    assert result is not None
    assert result.trades >= 2
