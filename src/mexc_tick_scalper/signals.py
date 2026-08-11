from __future__ import annotations

from collections.abc import Sequence


def momentum_direction(prices: Sequence[float], ticks: int) -> int:
    """Return +1/-1 only for a strictly monotonic run of the last N price changes."""
    if ticks <= 0 or len(prices) < ticks + 1:
        return 0
    window = prices[-(ticks + 1):]
    diffs = [b - a for a, b in zip(window, window[1:])]
    if all(delta > 0 for delta in diffs):
        return 1
    if all(delta < 0 for delta in diffs):
        return -1
    return 0
