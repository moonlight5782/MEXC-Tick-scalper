from __future__ import annotations

from math import inf
from typing import Iterable

from .exit_logic import TickExitTracker
from .models import ShadowResult, Tick


def _direction(prices: list[float], n: int) -> int:
    if len(prices) < n + 1:
        return 0
    window = prices[-(n + 1):]
    diffs = [b - a for a, b in zip(window, window[1:])]
    if all(d > 0 for d in diffs):
        return 1
    if all(d < 0 for d in diffs):
        return -1
    return 0


def replay(symbol: str, ticks: Iterable[Tick], momentum_ticks: int, reversal_ticks: int, max_hold_seconds: int = 180) -> ShadowResult:
    seq = list(ticks)
    if len(seq) < momentum_ticks + 3:
        return ShadowResult(symbol, momentum_ticks, reversal_ticks, 0, 0, 0, 0.0, 0.0, 0.0, 0.0, 0.0)

    prices: list[float] = []
    tracker: TickExitTracker | None = None
    entry = 0.0
    entry_ts = 0
    pnl_bps: list[float] = []

    for tick in seq:
        prices.append(tick.price)

        if tracker is None:
            d = _direction(prices, momentum_ticks)
            if d == 0:
                continue
            entry = tick.price
            entry_ts = tick.ts_ms
            tracker = TickExitTracker(
                side=d,
                entry_price=entry,
                reversal_ticks=reversal_ticks,
            )
            continue

        timed_out = tick.ts_ms - entry_ts >= max_hold_seconds * 1000
        should_exit = tracker.on_tick(tick.price)
        if should_exit or timed_out:
            raw = tracker.side * (tick.price - entry) / entry * 10_000.0
            pnl_bps.append(raw)
            tracker = None

    wins = [x for x in pnl_bps if x > 0]
    losses = [x for x in pnl_bps if x < 0]
    gross_profit = sum(wins)
    gross_loss = -sum(losses)
    pf = gross_profit / gross_loss if gross_loss > 0 else (inf if gross_profit > 0 else 0.0)
    expectancy = sum(pnl_bps) / len(pnl_bps) if pnl_bps else 0.0

    equity = 0.0
    peak = 0.0
    max_dd = 0.0
    for p in pnl_bps:
        equity += p
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)

    return ShadowResult(
        symbol=symbol,
        momentum_ticks=momentum_ticks,
        reversal_ticks=reversal_ticks,
        trades=len(pnl_bps),
        wins=len(wins),
        losses=len(losses),
        gross_profit_bps=gross_profit,
        gross_loss_bps=gross_loss,
        expectancy_bps=expectancy,
        profit_factor=pf,
        max_drawdown_bps=max_dd,
    )


def best_result(symbol: str, ticks: Iterable[Tick], momentum_grid: list[int], reversal_grid: list[int], max_hold_seconds: int, min_trades: int) -> ShadowResult | None:
    cached = list(ticks)
    candidates: list[ShadowResult] = []
    for m in momentum_grid:
        for r in reversal_grid:
            result = replay(symbol, cached, m, r, max_hold_seconds=max_hold_seconds)
            if result.trades >= min_trades:
                candidates.append(result)
    if not candidates:
        return None
    return max(candidates, key=lambda x: (x.expectancy_bps, x.profit_factor, -x.max_drawdown_bps))
