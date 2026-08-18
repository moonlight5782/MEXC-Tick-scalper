from __future__ import annotations

import asyncio
import math
import statistics
import time
from collections import deque
from dataclasses import dataclass

from .web_execution import MexcWebExecutionAdapter, WebExecutionConfig


@dataclass(frozen=True, slots=True)
class LatencySnapshot:
    measured_at: float
    samples: int
    latest_ms: float
    median_ms: float
    p75_ms: float
    p95_ms: float
    in_flight_ms: float

    def age_ms(self, now: float | None = None) -> float:
        now = time.monotonic() if now is None else now
        return max(0.0, now - self.measured_at) * 1000.0

    @property
    def effective_ms(self) -> float:
        # A current spike/stall must never be hidden by a rolling percentile.
        return max(self.latest_ms, self.p75_ms, self.in_flight_ms)


class RealtimeExecutionLatency:
    """Current MEXC private-path latency proxy with spike/stall protection.

    This is deliberately labelled a proxy: a read RTT is not an IOC fill RTT.
    Testnet order telemetry can later calibrate the multiplier/distribution, but
    the live shadow must never use a fixed latency constant or hide a current
    request stall behind a smoothed percentile.
    """

    def __init__(self, *, interval_ms: float = 250.0, window: int = 31, minimum_samples: int = 5) -> None:
        self.interval_ms = max(100.0, float(interval_ms))
        self.window = max(5, int(window))
        self.minimum_samples = max(3, min(int(minimum_samples), self.window))
        self._samples: deque[tuple[float, float]] = deque(maxlen=self.window)
        self._in_flight_started: float | None = None
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

    def snapshot(self, now: float | None = None) -> LatencySnapshot | None:
        if len(self._samples) < self.minimum_samples:
            return None
        now = time.monotonic() if now is None else now
        values = [v for _, v in self._samples]
        measured_at, latest = self._samples[-1]
        inflight = 0.0 if self._in_flight_started is None else max(0.0, now - self._in_flight_started) * 1000.0
        return LatencySnapshot(
            measured_at=measured_at,
            samples=len(values),
            latest_ms=latest,
            median_ms=statistics.median(values),
            p75_ms=self._percentile(values, 0.75),
            p95_ms=self._percentile(values, 0.95),
            in_flight_ms=inflight,
        )

    def fresh_effective_ms(self, *, max_age_ms: float = 2000.0) -> tuple[float, float] | None:
        snap = self.snapshot()
        if snap is None or snap.age_ms() > max_age_ms:
            return None
        return snap.effective_ms, snap.age_ms()

    def best_effort_exit_ms(self) -> tuple[float, float, bool] | None:
        snap = self.snapshot()
        if snap is None:
            return None
        return snap.effective_ms, snap.age_ms(), False

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="canonical-mexc-latency")

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
                self._in_flight_started = time.monotonic()
                started_ns = time.perf_counter_ns()
                try:
                    await adapter.get_positions()
                    elapsed = (time.perf_counter_ns() - started_ns) / 1_000_000.0
                    if elapsed > 0 and math.isfinite(elapsed):
                        self._samples.append((time.monotonic(), elapsed))
                    self.last_error = None
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self.last_error = f"{type(exc).__name__}: {exc}"
                finally:
                    self._in_flight_started = None
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=self.interval_ms / 1000.0)
                except TimeoutError:
                    pass
