from __future__ import annotations

import csv
from pathlib import Path

from mexc_tick_scalper.persistent_lag_profile import build_profiles, select_profiles


FIELDS = [
    "event_ms", "signal_id", "event", "symbol", "direction", "elapsed_ms",
    "measured_rtt_ms", "signal_residual_bps", "current_residual_bps",
    "signal_threshold_bps", "signal_noise_bps", "signal_spread_bps",
    "current_spread_bps", "leader_advantage_bps", "rtt_survived",
    "fill_ratio", "terminal_reason", "lifetime_ms",
]


def add_signal(rows, sid: str, symbol: str, residual: float, threshold: float, lead: float, lifetime: float, rtt: float = 300.0):
    rows.append({
        "signal_id": sid, "event": "signal", "symbol": symbol,
        "measured_rtt_ms": rtt, "signal_residual_bps": residual,
        "signal_threshold_bps": threshold, "leader_advantage_bps": lead,
    })
    rows.append({
        "signal_id": sid, "event": "terminal", "symbol": symbol,
        "measured_rtt_ms": rtt, "signal_residual_bps": residual,
        "signal_threshold_bps": threshold, "leader_advantage_bps": lead,
        "terminal_reason": "convergence", "lifetime_ms": lifetime,
    })


def write_rows(path: Path, rows):
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k, "") for k in FIELDS})


def test_profiles_separate_persistent_from_fast_pair(tmp_path: Path):
    rows = []
    for i, life in enumerate((420, 500, 600, 800), 1):
        add_signal(rows, f"p{i}", "PERSIST_USDT", 8.0, 3.0, 6.0, life)
    for i, life in enumerate((40, 80, 120, 150), 1):
        add_signal(rows, f"f{i}", "FAST_USDT", 3.0, 2.0, 2.0, life)
    path = tmp_path / "life.csv"
    write_rows(path, rows)

    profiles = build_profiles(path)
    selected = select_profiles(profiles)

    assert [p.symbol for p in selected] == ["PERSIST_USDT"]
    persistent = next(p for p in profiles if p.symbol == "PERSIST_USDT")
    assert persistent.signals == 4
    assert persistent.survive_execution_rate == 1.0
    assert persistent.median_signal_strength_ratio > 2.0


def test_strength_filter_rejects_barely_above_threshold(tmp_path: Path):
    rows = []
    for i in range(4):
        add_signal(rows, f"s{i}", "WEAK_USDT", 3.1, 3.0, 3.0, 600.0)
    path = tmp_path / "life.csv"
    write_rows(path, rows)

    selected = select_profiles(build_profiles(path), min_signal_strength_ratio=1.5)
    assert selected == []
