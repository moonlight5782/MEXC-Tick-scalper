from __future__ import annotations

import csv
from pathlib import Path
from typing import Iterable

from .models import Tick


CSV_HEADER = ["symbol", "price", "volume", "side", "ts_ms"]


def append_tick_csv(path: str | Path, tick: Tick) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    exists = target.exists() and target.stat().st_size > 0
    with target.open("a", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        if not exists:
            writer.writerow(CSV_HEADER)
        writer.writerow([tick.symbol, tick.price, tick.volume, tick.side, tick.ts_ms])


def write_ticks_csv(path: str | Path, ticks: Iterable[Tick]) -> int:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with target.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(CSV_HEADER)
        for tick in ticks:
            writer.writerow([tick.symbol, tick.price, tick.volume, tick.side, tick.ts_ms])
            count += 1
    return count


def load_ticks_csv(path: str | Path, symbol: str | None = None) -> list[Tick]:
    wanted = symbol.upper() if symbol else None
    ticks: list[Tick] = []
    with Path(path).open("r", newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        required = set(CSV_HEADER)
        if not required.issubset(set(reader.fieldnames or [])):
            raise ValueError(f"tick CSV must contain columns: {', '.join(CSV_HEADER)}")
        for row in reader:
            row_symbol = str(row["symbol"]).upper()
            if wanted and row_symbol != wanted:
                continue
            ticks.append(
                Tick(
                    symbol=row_symbol,
                    price=float(row["price"]),
                    volume=float(row["volume"]),
                    side=int(row["side"]),
                    ts_ms=int(float(row["ts_ms"])),
                )
            )
    ticks.sort(key=lambda x: x.ts_ms)
    return ticks
