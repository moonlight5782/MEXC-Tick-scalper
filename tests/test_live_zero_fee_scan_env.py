from pathlib import Path

import mexc_tick_scalper.live_zero_fee_scan as scan


def test_live_zero_fee_scan_loads_project_env(monkeypatch):
    calls = []

    def fake_load_dotenv(path, *, override):
        calls.append((Path(path), override))
        return True

    monkeypatch.setattr(scan, "load_dotenv", fake_load_dotenv)
    scan._load_env()

    assert len(calls) == 1
    path, override = calls[0]
    assert path.name == ".env"
    assert path.parent.name == "MEXC-Tick-scalper" or path.parent.exists()
    assert override is False
