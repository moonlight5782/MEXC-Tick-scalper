import time

from mexc_tick_scalper.realtime_latency import RealtimeLatencyProbe


def test_snapshot_marks_absurd_inflight_as_timed_out():
    probe = RealtimeLatencyProbe(interval_ms=100, window=5, minimum_samples=3, request_timeout_seconds=2.0)
    now = time.monotonic()
    probe._samples.extend([(now - 0.3, 300.0), (now - 0.2, 310.0), (now - 0.1, 320.0)])
    probe._inflight_started = now - 10.0
    snap = probe.snapshot()
    assert snap is not None
    assert snap.inflight_timed_out is True
    assert snap.inflight_ms == 0.0
    assert probe.current_ms(profile="latest", max_age_seconds=1.0) is None
