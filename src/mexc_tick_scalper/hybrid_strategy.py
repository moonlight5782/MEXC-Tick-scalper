from __future__ import annotations

from collections import deque
from dataclasses import dataclass

from .models import Tick


def _clamp(value: float, low: float = -1.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def _ema(values: list[float], span: int) -> float:
    if not values:
        return 0.0
    alpha = 2.0 / (max(1, span) + 1.0)
    out = values[0]
    for value in values[1:]:
        out = alpha * value + (1.0 - alpha) * out
    return out


@dataclass(slots=True)
class MicrostructureSnapshot:
    direction: int
    confidence: float
    trade_rate: float
    buy_ratio: float
    cvd_norm: float
    momentum_bps: float
    price_changes: int


class MicrostructureSignal:
    """Lightweight MEXC trade-flow signal inspired by smallfish."""

    def __init__(self, window_seconds: float = 5.0, min_trade_rate: float = 0.5) -> None:
        self.window_ms = max(1000, int(window_seconds * 1000))
        self.min_trade_rate = max(0.0, float(min_trade_rate))
        self.ticks: deque[Tick] = deque(maxlen=20_000)

    def update(self, tick: Tick) -> MicrostructureSnapshot:
        self.ticks.append(tick)
        cutoff = tick.ts_ms - self.window_ms
        while self.ticks and self.ticks[0].ts_ms < cutoff:
            self.ticks.popleft()

        rows = list(self.ticks)
        if len(rows) < 4:
            return MicrostructureSnapshot(0, 0.0, 0.0, 0.5, 0.0, 0.0, 0)

        elapsed = max(0.25, (rows[-1].ts_ms - rows[0].ts_ms) / 1000.0)
        trade_rate = len(rows) / elapsed

        buy_vol = sum(max(0.0, t.volume) for t in rows if t.side == 1)
        sell_vol = sum(max(0.0, t.volume) for t in rows if t.side == 2)
        total_vol = buy_vol + sell_vol
        buy_ratio = buy_vol / total_vol if total_vol > 0 else 0.5
        cvd_norm = (buy_vol - sell_vol) / total_vol if total_vol > 0 else 0.0

        prices: list[float] = []
        for t in rows:
            if not prices or t.price != prices[-1]:
                prices.append(t.price)
        price_changes = max(0, len(prices) - 1)
        if len(prices) < 3:
            return MicrostructureSnapshot(0, 0.0, trade_rate, buy_ratio, cvd_norm, 0.0, price_changes)

        fast = _ema(prices[-8:], span=3)
        slow = _ema(prices[-16:], span=7)
        momentum_bps = ((fast - slow) / slow) * 10_000 if slow > 0 else 0.0

        momentum_score = _clamp(momentum_bps / 2.0)
        cvd_score = _clamp(cvd_norm)
        flow_score = _clamp((buy_ratio - 0.5) / 0.20)
        raw = 0.45 * momentum_score + 0.35 * cvd_score + 0.20 * flow_score

        if trade_rate < self.min_trade_rate:
            return MicrostructureSnapshot(0, 0.0, trade_rate, buy_ratio, cvd_norm, momentum_bps, price_changes)

        confidence = min(1.0, abs(raw))
        direction = 1 if raw > 0 else -1 if raw < 0 else 0
        return MicrostructureSnapshot(direction, confidence, trade_rate, buy_ratio, cvd_norm, momentum_bps, price_changes)


@dataclass(slots=True)
class AsymmetricExitPolicy:
    """Fast loss cutting with price-confirmed management of established winners.

    A winner is not closed merely because trade-flow briefly flips. For LONG, a
    fresh higher high confirms that the upward move is still alive; for SHORT, a
    fresh lower low does the same. A signal-flip exit therefore needs both several
    opposite snapshots and an actual pullback from the best price reached.
    """

    side: int
    entry_price: float
    early_adverse_changes: int = 2
    liq_buffer_fraction: float = 0.25
    winner_arm_bps: float = 0.5
    winner_pullback_bps: float = 1.5
    winner_flip_pullback_bps: float = 0.5
    flip_confidence: float = 0.30
    fade_confidence: float = 0.12
    min_hold_seconds: float = 0.35
    winner_flip_confirmations: int = 3
    last_price: float | None = None
    extreme_price: float | None = None
    adverse_count: int = 0
    winner_armed: bool = False
    winner_opposite_count: int = 0

    def __post_init__(self) -> None:
        if self.side not in (1, -1):
            raise ValueError("side must be +1 or -1")
        if self.entry_price <= 0:
            raise ValueError("entry_price must be positive")
        if self.early_adverse_changes < 1:
            raise ValueError("early_adverse_changes must be >= 1")
        if self.winner_flip_confirmations < 1:
            raise ValueError("winner_flip_confirmations must be >= 1")
        if self.winner_flip_pullback_bps < 0:
            raise ValueError("winner_flip_pullback_bps must be >= 0")
        if not 0 < self.liq_buffer_fraction < 1:
            raise ValueError("liq_buffer_fraction must be between 0 and 1")
        self.last_price = self.entry_price
        self.extreme_price = self.entry_price

    def _near_liquidation(self, price: float, liquidation_price: float | None) -> bool:
        if liquidation_price is None or liquidation_price <= 0:
            return False
        if self.side == 1 and liquidation_price < self.entry_price:
            trigger = liquidation_price + (self.entry_price - liquidation_price) * self.liq_buffer_fraction
            return price <= trigger
        if self.side == -1 and liquidation_price > self.entry_price:
            trigger = liquidation_price - (liquidation_price - self.entry_price) * self.liq_buffer_fraction
            return price >= trigger
        return False

    def _signed_return_bps(self, price: float) -> float:
        return self.side * (price - self.entry_price) / self.entry_price * 10_000.0

    def _pullback_bps(self, price: float) -> float:
        assert self.extreme_price is not None
        if self.side == 1:
            return max(0.0, (self.extreme_price - price) / self.extreme_price * 10_000.0)
        return max(0.0, (price - self.extreme_price) / self.extreme_price * 10_000.0)

    def on_tick(
        self,
        *,
        price: float,
        liquidation_price: float | None,
        signal: MicrostructureSnapshot,
        age_seconds: float,
    ) -> str | None:
        if price <= 0:
            return None
        if self._near_liquidation(price, liquidation_price):
            return "liquidation_guard"

        assert self.last_price is not None
        assert self.extreme_price is not None

        signed_bps = self._signed_return_bps(price)
        if signed_bps >= self.winner_arm_bps:
            self.winner_armed = True

        made_new_favorable_extreme = (
            (self.side == 1 and price > self.extreme_price)
            or (self.side == -1 and price < self.extreme_price)
        )
        if made_new_favorable_extreme:
            self.extreme_price = price
            # Price itself confirms continuation. Any accumulated opposite-flow
            # confirmations are stale once LONG makes a higher high / SHORT a lower low.
            self.winner_opposite_count = 0

        if price != self.last_price:
            delta = price - self.last_price
            favorable = delta > 0 if self.side == 1 else delta < 0
            if favorable:
                self.adverse_count = 0
            elif not self.winner_armed:
                self.adverse_count += 1
            self.last_price = price

        opposite = signal.direction == -self.side and signal.confidence >= self.flip_confidence
        supportive = signal.direction == self.side and signal.confidence >= self.fade_confidence

        if not self.winner_armed:
            if age_seconds >= self.min_hold_seconds and opposite:
                return "early_signal_flip"
            if self.adverse_count >= self.early_adverse_changes:
                return "early_adverse_cut"
            return None

        # For an established winner, flow alone cannot close the position while
        # price is still extending in our direction. We count opposite flow only
        # after the latest favorable extreme and require an actual price pullback.
        if opposite and not made_new_favorable_extreme:
            self.winner_opposite_count += 1
        elif not opposite:
            self.winner_opposite_count = 0

        pullback_bps = self._pullback_bps(price)
        if (
            age_seconds >= self.min_hold_seconds
            and self.winner_opposite_count >= self.winner_flip_confirmations
            and pullback_bps >= self.winner_flip_pullback_bps
        ):
            return "winner_signal_flip_price_confirmed"

        # Larger giveback + loss of supportive flow remains a separate trailing exit.
        if pullback_bps >= self.winner_pullback_bps and not supportive:
            return "winner_pullback_fade"
        return None


HoldUntilAgainstExit = AsymmetricExitPolicy
