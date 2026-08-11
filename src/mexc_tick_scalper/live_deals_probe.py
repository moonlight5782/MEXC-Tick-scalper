from __future__ import annotations

import argparse
import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from rich.console import Console
from rich.table import Table

from .web_execution import MexcWebError, MexcWebExecutionAdapter, WebExecutionConfig

console = Console()
MOLDOVA_TZ = timezone(timedelta(hours=3))


def _to_epoch_ms(value: str) -> int:
    dt = datetime.strptime(value, "%Y-%m-%d %H:%M:%S").replace(tzinfo=MOLDOVA_TZ)
    return int(dt.timestamp() * 1000)


def _extract_rows(payload: Any) -> list[dict[str, Any]]:
    data = payload.get("data", payload) if isinstance(payload, dict) else payload
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    if isinstance(data, dict):
        for key in ("resultList", "list", "rows", "items"):
            value = data.get(key)
            if isinstance(value, list):
                return [x for x in value if isinstance(x, dict)]
    return []


async def run(args: argparse.Namespace) -> None:
    load_dotenv()
    cfg = WebExecutionConfig.from_env(write_enabled=False)
    start_ms = _to_epoch_ms(args.start)
    end_ms = _to_epoch_ms(args.end)
    if end_ms <= start_ms:
        raise MexcWebError("end must be after start")

    params = {
        "symbol": args.symbol.upper(),
        "start_time": start_ms,
        "end_time": end_ms,
        "page_num": 1,
        "page_size": args.page_size,
    }
    async with MexcWebExecutionAdapter(cfg) as adapter:
        payload = await adapter._request("GET", "/private/order/list/order_deals", params=params)

    rows = _extract_rows(payload)
    table = Table(title=f"Live MEXC order deals: {args.symbol.upper()} ({len(rows)} rows)")
    for name in ("orderId", "symbol", "side", "price", "vol", "fee", "isTaker", "time"):
        table.add_column(name)
    for row in rows[:50]:
        table.add_row(*[str(row.get(name, "")) for name in ("orderId", "symbol", "side", "price", "vol", "fee", "isTaker", "time")])
    console.print(table)

    output = Path(args.output)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    console.print(f"Saved raw response to {output}")
    if not rows:
        console.print("[yellow]No fills returned for that interval.[/yellow]")


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only probe of MEXC Futures historical order fills")
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--start", required=True, help="Moldova local time YYYY-MM-DD HH:MM:SS")
    parser.add_argument("--end", required=True, help="Moldova local time YYYY-MM-DD HH:MM:SS")
    parser.add_argument("--page-size", type=int, default=1000)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    try:
        asyncio.run(run(args))
    except MexcWebError as exc:
        console.print(f"[red]LIVE DEALS PROBE FAILED:[/red] {exc}")
        raise SystemExit(2) from exc


if __name__ == "__main__":
    main()
