from pathlib import Path

from mexc_tick_scalper.backtest import walk_forward
from mexc_tick_scalper.models import Tick
from mexc_tick_scalper.tick_data import load_ticks_csv, write_ticks_csv


def _ticks(cycles: int = 30) -> list[Tick]:
    prices = []
    for _ in range(cycles):
        prices.extend([100.0, 100.1, 100.2, 100.3, 100.4, 100.3, 100.2, 100.1])
    return [Tick("TEST_USDT", p, 1.0, 1, i * 100) for i, p in enumerate(prices)]


def test_tick_csv_roundtrip(tmp_path: Path):
    path = tmp_path / "ticks.csv"
    original = _ticks(2)
    assert write_ticks_csv(path, original) == len(original)
    loaded = load_ticks_csv(path, symbol="TEST_USDT")
    assert len(loaded) == len(original)
    assert loaded[3].price == original[3].price
    assert loaded[3].ts_ms == original[3].ts_ms


def test_walk_forward_uses_train_params_on_validation():
    result = walk_forward(
        symbol="TEST_USDT",
        ticks=_ticks(),
        momentum_grid=[2, 3],
        reversal_grid=[1, 2],
        max_hold_seconds=30,
        min_train_trades=3,
        train_fraction=0.7,
    )
    assert result is not None
    assert result.train.trades >= 3
    assert result.validation.momentum_ticks == result.train.momentum_ticks
    assert result.validation.reversal_ticks == result.train.reversal_ticks
