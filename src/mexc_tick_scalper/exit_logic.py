from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class TickExitTracker:
    """First-adverse-tick trailing exit state.

    side: +1 for long, -1 for short.
    reversal_ticks: how many consecutive adverse ticks from the current extreme
    are required before an exit signal is emitted. Default strategy uses 1.
    """

    side: int
    entry_price: float
    reversal_ticks: int = 1
    extreme_price: float | None = None
    adverse_count: int = 0

    def __post_init__(self) -> None:
        if self.side not in (1, -1):
            raise ValueError("side must be +1 (long) or -1 (short)")
        if self.reversal_ticks < 1:
            raise ValueError("reversal_ticks must be >= 1")
        if self.entry_price <= 0:
            raise ValueError("entry_price must be > 0")
        if self.extreme_price is None:
            self.extreme_price = self.entry_price

    def on_tick(self, price: float) -> bool:
        """Return True when the position should be closed."""
        if price <= 0:
            return False

        assert self.extreme_price is not None
        if self.side == 1:
            if price > self.extreme_price:
                self.extreme_price = price
                self.adverse_count = 0
                return False
            if price < self.extreme_price:
                self.adverse_count += 1
                return self.adverse_count >= self.reversal_ticks
        else:
            if price < self.extreme_price:
                self.extreme_price = price
                self.adverse_count = 0
                return False
            if price > self.extreme_price:
                self.adverse_count += 1
                return self.adverse_count >= self.reversal_ticks

        # Equal-price ticks neither improve nor worsen the extreme.
        self.adverse_count = 0
        return False
