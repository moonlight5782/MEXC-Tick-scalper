from __future__ import annotations

import argparse
import csv
import json
import statistics
from collections import defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class PairLagProfile:
    symbol: str
    signals: int
    median_lifetime_ms: float
    p75_lifetime_ms: float
    p90_lifetime_ms: float
    survive_execution_rate: float
    convergence_rate: float
    reversal_rate: float
    median_signal_residual_bps: float
    median_signal_strength_ratio: float
    median_leader_advantage_bps: float

    def eligible(
        self,
        *,
        min_signals: int,
        min_median_lifetime_ms: float,
        min_survival_rate: float,
        min_signal_strength_ratio: float,
    ) -> bool:
        return (
            self.signals >= min_signals
            and self.median_lifetime_ms >= min_median_lifetime_ms
            and self.survive_execution_rate >= min_survival_rate
            and self.median_signal_strength_ratio >= min_signal_strength_ratio
        )


def _f(row: dict[str, str], key: str) -> float | None:
    raw = (row.get(key) or "").strip()
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def _percentile(values: list[float], q: float) -> float:
    rows = sorted(values)
    if not rows:
        return 0.0
    if len(rows) == 1:
        return rows[0]
    pos = (len(rows) - 1) * q
    lo = int(pos)
    hi = min(lo + 1, len(rows) - 1)
    frac = pos - lo
    return rows[lo] * (1.0 - frac) + rows[hi] * frac


def build_profiles(path: Path) -> list[PairLagProfile]:
    """Build per-symbol persistence statistics from independent lifetime diagnostic CSV."""
    rows_by_signal: dict[str, dict[str, dict[str, str]]] = defaultdict(dict)
    with path.open("r", newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            signal_id = (row.get("signal_id") or "").strip()
            event = (row.get("event") or "").strip()
            if signal_id and event in {"signal", "terminal"}:
                rows_by_signal[signal_id][event] = row

    by_symbol: dict[str, list[tuple[dict[str, str], dict[str, str]]]] = defaultdict(list)
    for events in rows_by_signal.values():
        signal = events.get("signal")
        terminal = events.get("terminal")
        if not signal or not terminal:
            continue
        symbol = (signal.get("symbol") or terminal.get("symbol") or "").strip().upper()
        if symbol:
            by_symbol[symbol].append((signal, terminal))

    profiles: list[PairLagProfile] = []
    for symbol, pairs in by_symbol.items():
        lifetimes: list[float] = []
        residuals: list[float] = []
        strengths: list[float] = []
        leads: list[float] = []
        survived = 0
        convergence = 0
        reversal = 0

        for signal, terminal in pairs:
            lifetime = _f(terminal, "lifetime_ms")
            measured_rtt = _f(terminal, "measured_rtt_ms") or _f(signal, "measured_rtt_ms")
            residual = _f(signal, "signal_residual_bps")
            threshold = _f(signal, "signal_threshold_bps")
            lead = _f(signal, "leader_advantage_bps")
            reason = (terminal.get("terminal_reason") or "").strip()
            if lifetime is None:
                continue
            lifetimes.append(lifetime)
            if measured_rtt is not None and lifetime >= measured_rtt:
                survived += 1
            if residual is not None:
                residuals.append(abs(residual))
            if residual is not None and threshold is not None and threshold > 0:
                strengths.append(abs(residual) / threshold)
            if lead is not None:
                leads.append(abs(lead))
            if reason == "convergence":
                convergence += 1
            elif reason == "residual_reversal":
                reversal += 1

        if not lifetimes:
            continue
        n = len(lifetimes)
        profiles.append(PairLagProfile(
            symbol=symbol,
            signals=n,
            median_lifetime_ms=statistics.median(lifetimes),
            p75_lifetime_ms=_percentile(lifetimes, 0.75),
            p90_lifetime_ms=_percentile(lifetimes, 0.90),
            survive_execution_rate=survived / n,
            convergence_rate=convergence / n,
            reversal_rate=reversal / n,
            median_signal_residual_bps=statistics.median(residuals) if residuals else 0.0,
            median_signal_strength_ratio=statistics.median(strengths) if strengths else 0.0,
            median_leader_advantage_bps=statistics.median(leads) if leads else 0.0,
        ))

    return sorted(
        profiles,
        key=lambda p: (
            p.survive_execution_rate,
            p.median_lifetime_ms,
            p.median_signal_strength_ratio,
            p.signals,
        ),
        reverse=True,
    )


def select_profiles(
    profiles: list[PairLagProfile],
    *,
    min_signals: int = 4,
    min_median_lifetime_ms: float = 300.0,
    min_survival_rate: float = 0.50,
    min_signal_strength_ratio: float = 1.50,
) -> list[PairLagProfile]:
    return [
        p for p in profiles
        if p.eligible(
            min_signals=min_signals,
            min_median_lifetime_ms=min_median_lifetime_ms,
            min_survival_rate=min_survival_rate,
            min_signal_strength_ratio=min_signal_strength_ratio,
        )
    ]


def latest_lifetime_csv(directory: Path) -> Path:
    matches = sorted(directory.glob("prelive_lag_lifetime_*.csv"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not matches:
        raise FileNotFoundError("No prelive_lag_lifetime_*.csv found")
    return matches[0]


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Rank Binance->MEXC pairs by observed lag persistence")
    p.add_argument("--csv", default="")
    p.add_argument("--min-signals", type=int, default=4)
    p.add_argument("--min-median-lifetime-ms", type=float, default=300.0)
    p.add_argument("--min-survival-rate", type=float, default=0.50)
    p.add_argument("--min-signal-strength-ratio", type=float, default=1.50)
    p.add_argument("--json", default="lag_pair_profile.json")
    return p


def main() -> None:
    args = build_parser().parse_args()
    source = Path(args.csv) if args.csv else latest_lifetime_csv(Path.cwd())
    profiles = build_profiles(source)
    selected = select_profiles(
        profiles,
        min_signals=args.min_signals,
        min_median_lifetime_ms=args.min_median_lifetime_ms,
        min_survival_rate=args.min_survival_rate,
        min_signal_strength_ratio=args.min_signal_strength_ratio,
    )

    print(f"LIFETIME CSV: {source.resolve()}")
    print("PAIR PERSISTENCE RANKING")
    for p in profiles:
        mark = "KEEP" if p in selected else "DROP"
        print(
            f"{mark:4} {p.symbol:18} n={p.signals:3d} "
            f"med={p.median_lifetime_ms:7.1f}ms p90={p.p90_lifetime_ms:7.1f}ms "
            f"survive={p.survive_execution_rate*100:5.1f}% strength={p.median_signal_strength_ratio:5.2f}x "
            f"residual={p.median_signal_residual_bps:6.2f}bps lead={p.median_leader_advantage_bps:6.2f}bps "
            f"conv/rev={p.convergence_rate*100:4.0f}/{p.reversal_rate*100:4.0f}%"
        )

    payload = {
        "source_csv": str(source.resolve()),
        "filters": {
            "min_signals": args.min_signals,
            "min_median_lifetime_ms": args.min_median_lifetime_ms,
            "min_survival_rate": args.min_survival_rate,
            "min_signal_strength_ratio": args.min_signal_strength_ratio,
        },
        "selected_symbols": [p.symbol for p in selected],
        "profiles": [asdict(p) for p in profiles],
    }
    Path(args.json).write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print("\nSELECTED PERSISTENT-LAG SYMBOLS")
    print(",".join(p.symbol for p in selected) or "NONE")
    print(f"Profile JSON: {Path(args.json).resolve()}")


if __name__ == "__main__":
    main()
