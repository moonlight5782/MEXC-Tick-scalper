from __future__ import annotations

import argparse
import asyncio
import csv
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import aiohttp
from rich.console import Console

console = Console()
MOLDOVA_TZ = timezone(timedelta(hours=3))


def _to_epoch_seconds(value: str) -> int:
    dt = datetime.strptime(value, "%Y-%m-%d %H:%M:%S").replace(tzinfo=MOLDOVA_TZ)
    return int(dt.timestamp())


def _rows(payload: Any) -> list[dict[str, Any]]:
    data = payload.get("data", payload) if isinstance(payload, dict) else payload
    if not isinstance(data, dict):
        return []
    times = data.get("time") or []
    opens = data.get("open") or []
    closes = data.get("close") or []
    highs = data.get("high") or []
    lows = data.get("low") or []
    vols = data.get("vol") or []
    amounts = data.get("amount") or []
    n = min(len(times), len(opens), len(closes), len(highs), len(lows))
    result: list[dict[str, Any]] = []
    for i in range(n):
        ts = int(times[i])
        result.append({
            "time": ts,
            "time_local": datetime.fromtimestamp(ts, MOLDOVA_TZ).strftime("%Y-%m-%d %H:%M:%S"),
            "open": float(opens[i]),
            "high": float(highs[i]),
            "low": float(lows[i]),
            "close": float(closes[i]),
            "vol": float(vols[i]) if i < len(vols) else None,
            "amount": float(amounts[i]) if i < len(amounts) else None,
        })
    return result


async def run(args: argparse.Namespace) -> None:
    start = _to_epoch_seconds(args.start)
    end = _to_epoch_seconds(args.end)
    if end <= start:
        raise SystemExit("end must be after start")

    base = args.base_url.rstrip("/")
    url = f"{base}/api/v1/contract/kline/{args.symbol.upper()}"
    params = {"interval": args.interval, "start": start, "end": end}
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as session:
        async with session.get(url, params=params) as response:
            text = await response.text()
            if response.status >= 400:
                raise RuntimeError(f"HTTP {response.status}: {text[:500]}")
            payload = json.loads(text)
    rows = _rows(payload)
    output = Path(args.output)
    if output.suffix.lower() == ".json":
        output.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    else:
        with output.open("w", newline="", encoding="utf-8-sig") as fh:
            writer = csv.DictWriter(fh, fieldnames=["time", "time_local", "open", "high", "low", "close", "vol", "amount"])
            writer.writeheader()
            writer.writerows(rows)
    console.print(f"Downloaded {len(rows)} {args.interval} candles for {args.symbol.upper()} -> {output}")
    if rows:
        console.print(f"First: {rows[0]['time_local']}  Last: {rows[-1]['time_local']}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Download public MEXC Futures historical candles")
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--start", required=True, help="Moldova local time YYYY-MM-DD HH:MM:SS")
    parser.add_argument("--end", required=True, help="Moldova local time YYYY-MM-DD HH:MM:SS")
    parser.add_argument("--interval", default="Min1")
    parser.add_argument("--output", required=True)
    parser.add_argument("--base-url", default="https://contract.mexc.com")
    args = parser.parse_args()
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
