from __future__ import annotations

from dataclasses import dataclass
from math import inf

from .models import ShadowResult, Tick
from .shadow import replay


@dataclass(slots=True)
class WalkForwardResult:
    symbol: str
    train: ShadowResult
    validation: ShadowResult
    train_ticks: int
    validation_ticks: int

    @property
    def passed(self) -> bool:
        return (
            self.validation.trades > 0
            and self.validation.expectancy_bps > 0
            and self.validation.profit_factor > 1.0
        )


def _candidate_key(result: ShadowResult) -> tuple[float, float, float]:
    pf = result.profit_factor if result.profit_factor != inf else 1_000_000.0
    return (result.expectancy_bps, pf, -result.max_drawdown_bps)


def walk_forward(
    *,
    symbol: str,
    ticks: list[Tick],
    momentum_grid: list[int],
    reversal_grid: list[int],
    max_hold_seconds: int,
    min_train_trades: int,
    train_fraction: float = 0.70,
) -> WalkForwardResult | None:
    if not 0.5 <= train_fraction < 1.0:
        raise ValueError("train_fraction must be in [0.5, 1.0)")
    if len(ticks) < 20:
        return None

    split = max(1, min(len(ticks) - 1, int(len(ticks) * train_fraction)))
    train_ticks = ticks[:split]
    validation_ticks = ticks[split:]

    candidates: list[ShadowResult] = []
    for momentum in momentum_grid:
        for reversal in reversal_grid:
            result = replay(
                symbol,
                train_ticks,
                momentum_ticks=momentum,
                reversal_ticks=reversal,
                max_hold_seconds=max_hold_seconds,
            )
            if result.trades >= min_train_trades:
                candidates.append(result)

    if not candidates:
        return None

    train_best = max(candidates, key=_candidate_key)
    validation = replay(
        symbol,
        validation_ticks,
        momentum_ticks=train_best.momentum_ticks,
        reversal_ticks=train_best.reversal_ticks,
        max_hold_seconds=max_hold_seconds,
    )
    return WalkForwardResult(
        symbol=symbol,
        train=train_best,
        validation=validation,
        train_ticks=len(train_ticks),
        validation_ticks=len(validation_ticks),
    )
