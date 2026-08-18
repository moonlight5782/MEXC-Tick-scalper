from mexc_tick_scalper.realtime_latency import RealtimeLatencyProbe


def test_realtime_probe_uses_rolling_robust_profiles(monkeypatch):
    probe = RealtimeLatencyProbe(interval_ms=250, window=5, minimum_samples=3)
    probe._samples.extend([
        (100.0, 100.0),
        (100.1, 120.0),
        (100.2, 140.0),
        (100.3, 160.0),
        (100.4, 1000.0),
    ])
    monkeypatch.setattr("mexc_tick_scalper.realtime_latency.time.monotonic", lambda: 100.5)

    snap = probe.snapshot()
    assert snap is not None
    assert snap.latest_ms == 1000.0
    assert snap.median_ms == 140.0
    assert snap.p75_ms == 160.0
    assert snap.p95_ms > 160.0
    assert probe.current_ms(profile="p75", max_age_seconds=1.0) == 160.0


def test_stale_measurement_blocks_new_entry_but_remains_available_for_exit(monkeypatch):
    probe = RealtimeLatencyProbe(interval_ms=250, window=5, minimum_samples=3)
    probe._samples.extend([
        (100.0, 100.0),
        (100.1, 120.0),
        (100.2, 140.0),
    ])
    monkeypatch.setattr("mexc_tick_scalper.realtime_latency.time.monotonic", lambda: 105.0)

    assert probe.current_ms(profile="p75", max_age_seconds=2.0) is None
    assert probe.last_known_ms(profile="p75") is not None
