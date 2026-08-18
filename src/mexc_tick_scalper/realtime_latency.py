from __future__ import annotations

import asyncio
import math
import statistics
import time
from collections import deque
from dataclasses import dataclass

from .web_execution import MexcWebExecutionAdapter, WebExecutionConfig


@dataclass(frozen=True, slots=True)
class RealtimeLatencySnapshot:
    measured_at: float
    samples: int
    latest_ms: float
    median_ms: float
    p75_ms: float
    p95_ms: float

    def age_seconds(self, now: float | None = None) -> float:
        current = time.monotonic() if now is None else now
        return max(0.0, current - self.measured_at)

    def value(self, profile: str) -> float:
        if profile == "latest":
            return self.latest_ms
        if profile == "median":
            return self.median_ms
        if profile == "p95":
            return self.p95_ms
        return self.p75_ms


class RealtimeLatencyProbe:
    """Continuously measure the current MEXC LIVE private request path.

    This is a transport-path proxy, not a claim that a read-only request has
    identical matching-engine latency to an IOC.  It replaces fixed historical
    constants with measurements from the current process/network/session.
    """

    def __init__(
        self,
        *,
        interval_ms: float = 250.0,
        window: int = 31,
        minimum_samples: int = 5,
    ) -> None:
        self.interval_ms = max(100.0, float(interval_ms))
        self.window = max(5, int(window))
        self.minimum_samples = max(3, min(int(minimum_samples), self.window))
        self._samples: deque[tuple[float, float]] = deque(maxlen=self.window)
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self.last_error: str | None = None

    @staticmethod
    def _percentile(values: list[float], q: float) -> float:
        rows = sorted(values)
        if not rows:
            return math.inf
        if len(rows) == 1:
            return rows[0]
        pos = (len(rows) - 1) * q
        lo = int(pos)
        hi = min(lo + 1, len(rows) - 1)
        frac = pos - lo
        return rows[lo] * (1.0 - frac) + rows[hi] * frac

    def snapshot(self) -> RealtimeLatencySnapshot | None:
        if len(self._samples) < self.minimum_samples:
            return None
        values = [value for _, value in self._samples]
        measured_at, latest = self._samples[-1]
        return RealtimeLatencySnapshot(
            measured_at=measured_at,
            samples=len(values),
            latest_ms=latest,
            median_ms=statistics.median(values),
            p75_ms=self._percentile(values, 0.75),
            p95_ms=self._percentile(values, 0.95),
        )

    def current_ms(self, *, profile: str = "p75", max_age_seconds: float = 2.0) -> float | None:
        snap = self.snapshot()
        if snap is None or snap.age_seconds() > max(0.1, float(max_age_seconds)):
            return None
        return snap.value(profile)

    def last_known_ms(self, *, profile: str = "p75") -> float | None:
        snap = self.snapshot()
        return snap.value(profile) if snap is not None else None

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="mexc-realtime-latency-probe")

    async def close(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _run(self) -> None:
        cfg = WebExecutionConfig.from_env(write_enabled=False)
        async with MexcWebExecutionAdapter(cfg) as adapter:
            while not self._stop.is_set():
                started_ns = time.perf_counter_ns()
                try:
                    await adapter.get_positions()
                    elapsed_ms = (time.perf_counter_ns() - started_ns) / 1_000_000.0
                    if elapsed_ms > 0 and math.isfinite(elapsed_ms):
                        self._samples.append((time.monotonic(), elapsed_ms))
                    self.last_error = None
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self.last_error = f"{type(exc).__name__}: {exc}"
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=self.interval_ms / 1000.0)
                except TimeoutError:
                    pass
