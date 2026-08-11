from __future__ import annotations

from collections.abc import Sequence


def _price_changes(prices: Sequence[float]) -> list[float]:
    """Collapse consecutive duplicate trade prices into actual price ticks."""
    changed: list[float] = []
    for price in prices:
        value = float(price)
        if not changed or value != changed[-1]:
            changed.append(value)
    return changed


def momentum_direction(prices: Sequence[float], ticks: int) -> int:
    """Return +1/-1 for a monotonic run of the last N *price changes*.

    Multiple trades can print at exactly the same price. Those duplicate trade
    events are not price ticks and must not break an otherwise valid momentum
    sequence.
    """
    if ticks <= 0:
        return 0
    changed = _price_changes(prices)
    if len(changed) < ticks + 1:
        return 0
    window = changed[-(ticks + 1):]
    diffs = [b - a for a, b in zip(window, window[1:])]
    if all(delta > 0 for delta in diffs):
        return 1
    if all(delta < 0 for delta in diffs):
        return -1
    return 0
