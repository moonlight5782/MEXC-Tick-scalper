from __future__ import annotations

import math
from dataclasses import dataclass


PROFIT_LOCK_FLOOR_BPS = 0.10


@dataclass(slots=True)
class ProfitHoldPolicy:
    """Own one position's winner state and ratcheting positive trailing stop."""

    distance_bps: float
    armed: bool = False
    peak_bps: float = 0.0
    stop_bps: float | None = None

    def update(self, executable_pnl_bps: float) -> float | None:
        move = float(executable_pnl_bps)
        self.peak_bps = max(self.peak_bps, move)

        candidate: float | None = None
        if self.peak_bps + 1e-9 >= 3.0:
            candidate = 0.5
        if self.peak_bps + 1e-9 >= 5.0:
            candidate = max(candidate or -math.inf, 2.0)
        if self.peak_bps + 1e-9 >= 6.0:
            candidate = max(candidate or -math.inf, self.peak_bps - max(0.1, self.distance_bps))

        if candidate is not None:
            self.stop_bps = candidate if self.stop_bps is None else max(self.stop_bps, candidate)

        if move > 0.0 and not self.armed:
            self.armed = True
            positive_floor = min(PROFIT_LOCK_FLOOR_BPS, move * 0.5)
            if positive_floor > 0.0:
                self.stop_bps = (
                    positive_floor
                    if self.stop_bps is None
                    else max(self.stop_bps, positive_floor)
                )

        return self.stop_bps

    @property
    def ordinary_thesis_exits_allowed(self) -> bool:
        return not self.armed
