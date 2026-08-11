from mexc_tick_scalper.signals import momentum_direction


def test_momentum_direction_detects_strict_runs():
    assert momentum_direction([100, 101, 102, 103], 3) == 1
    assert momentum_direction([103, 102, 101, 100], 3) == -1


def test_momentum_direction_rejects_flat_or_mixed_runs():
    assert momentum_direction([100, 101, 101, 102], 3) == 0
    assert momentum_direction([100, 101, 100, 102], 3) == 0
    assert momentum_direction([100, 101], 3) == 0
