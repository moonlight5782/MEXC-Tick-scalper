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
    inflight_ms: float = 0.0
    inflight_timed_out: bool = False

    def age_seconds(self, now: float | None = None) -> float:
        current = time.monotonic() if now is None else now
        return max(0.0, current - self.measured_at)

    def value(self, profile: str) -> float:
        if profile == "latest":
            base = self.latest_ms
        elif profile == "median":
            base = self.median_ms
        elif profile == "p95":
            base = self.p95_ms
        else:
            base = self.p75_ms
        # An actively running request is a lower bound on current RTT, but a
        # request that crossed the hard probe timeout is invalid as a latency
        # estimate and must cause the caller to wait for a fresh sample instead.
        return max(base, self.latest_ms, self.inflight_ms)


class RealtimeLatencyProbe:
    """Continuously measure the current MEXC LIVE private request path.

    This is a transport-path proxy, not a claim that a read-only request has
    identical matching-engine latency to an IOC. It replaces fixed historical
    constants with measurements from the current process/network/session.

    Each probe request has a hard timeout. This is important on laptops/desktops:
    sleep/resume or a stuck HTTP request must never turn into a multi-hour
    synthetic latency sample. Timed-out probes are discarded and the strategy
    waits for a fresh successful measurement before opening a new position.
    """

    def __init__(
        self,
        *,
        interval_ms: float = 250.0,
        window: int = 31,
        minimum_samples: int = 5,
        request_timeout_seconds: float = 2.0,
    ) -> None:
        self.interval_ms = max(100.0, float(interval_ms))
        self.window = max(5, int(window))
        self.minimum_samples = max(3, min(int(minimum_samples), self.window))
        self.request_timeout_seconds = max(0.5, float(request_timeout_seconds))
        self._samples: deque[tuple[float, float]] = deque(maxlen=self.window)
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._inflight_started: float | None = None
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
        now = time.monotonic()
        raw_inflight_ms = (
            max(0.0, (now - self._inflight_started) * 1000.0)
            if self._inflight_started is not None
            else 0.0
        )
        timeout_ms = self.request_timeout_seconds * 1000.0
        inflight_timed_out = raw_inflight_ms >= timeout_ms
        # Never surface a multi-hour/sleep-resume elapsed value as executable
        # latency. The timeout flag makes the snapshot unusable for new entries.
        inflight_ms = 0.0 if inflight_timed_out else raw_inflight_ms
        return RealtimeLatencySnapshot(
            measured_at=measured_at,
            samples=len(values),
            latest_ms=latest,
            median_ms=statistics.median(values),
            p75_ms=self._percentile(values, 0.75),
            p95_ms=self._percentile(values, 0.95),
            inflight_ms=inflight_ms,
            inflight_timed_out=inflight_timed_out,
        )

    def current_ms(self, *, profile: str = "p75", max_age_seconds: float = 2.0) -> float | None:
        snap = self.snapshot()
        if (
            snap is None
            or snap.inflight_timed_out
            or snap.age_seconds() > max(0.1, float(max_age_seconds))
        ):
            return None
        return snap.value(profile)

    def last_known_ms(self, *, profile: str = "p75") -> float | None:
        snap = self.snapshot()
        if snap is None or snap.inflight_timed_out:
            return None
        return snap.value(profile)

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
        self._inflight_started = None

    async def _run(self) -> None:
        cfg = WebExecutionConfig.from_env(write_enabled=False)
        async with MexcWebExecutionAdapter(cfg) as adapter:
            while not self._stop.is_set():
                started_ns = time.perf_counter_ns()
                self._inflight_started = time.monotonic()
                try:
                    await asyncio.wait_for(
                        adapter.get_positions(),
                        timeout=self.request_timeout_seconds,
                    )
                    elapsed_ms = (time.perf_counter_ns() - started_ns) / 1_000_000.0
                    if (
                        elapsed_ms > 0
                        and math.isfinite(elapsed_ms)
                        and elapsed_ms < self.request_timeout_seconds * 1000.0
                    ):
                        self._samples.append((time.monotonic(), elapsed_ms))
                    self.last_error = None
                except asyncio.CancelledError:
                    raise
                except TimeoutError:
                    self.last_error = f"probe_timeout>{self.request_timeout_seconds:.1f}s"
                except Exception as exc:
                    self.last_error = f"{type(exc).__name__}: {exc}"
                finally:
                    self._inflight_started = None
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=self.interval_ms / 1000.0)
                except TimeoutError:
                    pass
