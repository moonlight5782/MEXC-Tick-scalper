from pathlib import Path

from mexc_tick_scalper.live_readonly_rtt_probe import _percentile


def test_percentile_interpolates_measured_rtt():
    rows = [10.0, 20.0, 30.0, 40.0]
    assert _percentile(rows, 0.50) == 25.0
    assert _percentile(rows, 0.95) > 30.0


def test_rtt_probe_is_structurally_read_only():
    source = Path("src/mexc_tick_scalper/live_readonly_rtt_probe.py").read_text(encoding="utf-8")
    assert "write_enabled=False" in source
    assert "write_enabled=True" not in source
    assert ".open_ioc(" not in source
    assert "close_market_reduce_only" not in source
    assert "close_position_snapshot_reduce_only" not in source
