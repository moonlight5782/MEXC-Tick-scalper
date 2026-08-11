from __future__ import annotations

import argparse
import asyncio
import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from rich.console import Console
from rich.table import Table

from .web_execution import MexcWebError, MexcWebExecutionAdapter, WebExecutionConfig

console = Console()


def _load_project_env() -> None:
    project_root = Path(__file__).resolve().parents[2]
    load_dotenv(project_root / ".env", override=False)


def _to_epoch_ms(value: str, tz_name: str = "Europe/Chisinau") -> int:
    dt = datetime.strptime(value, "%Y-%m-%d %H:%M:%S").replace(tzinfo=ZoneInfo(tz_name))
    return int(dt.timestamp() * 1000)


def _rows(payload):
    if not isinstance(payload, dict):
        return []
    data = payload.get("data")
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("resultList", "list", "rows"):
            value = data.get(key)
            if isinstance(value, list):
                return value
    return []


async def fetch_history(adapter: MexcWebExecutionAdapter, *, symbol: str, start_ms: int, end_ms: int, max_pages: int = 50):
    result = []
    for page in range(1, max_pages + 1):
        payload = await adapter._request(
            "GET",
            "/private/order/list/history_orders",
            params={
                "symbol": symbol,
                "states": "3,4,5",
                "start_time": start_ms,
                "end_time": end_ms,
                "page_num": page,
                "page_size": 100,
            },
        )
        page_rows = _rows(payload)
        if not page_rows:
            break
        result.extend(page_rows)
        if len(page_rows) < 100:
            break
    return result


async def run(args: argparse.Namespace) -> None:
    _load_project_env()
    cfg = WebExecutionConfig.from_env(write_enabled=False)
    adapter = MexcWebExecutionAdapter(cfg)
    start_ms = _to_epoch_ms(args.start)
    end_ms = _to_epoch_ms(args.end)
    try:
        rows = await fetch_history(adapter, symbol=args.symbol.upper(), start_ms=start_ms, end_ms=end_ms)
    finally:
        await adapter.close()

    table = Table(title=f"Live MEXC history probe: {args.symbol.upper()} ({len(rows)} rows)")
    for col in ("orderId", "createTime", "updateTime", "side", "orderType", "vol", "dealVol", "dealAvgPrice", "usedMargin", "profit", "takerFee", "makerFee", "state"):
        table.add_column(col)
    for row in rows[: args.show]:
        table.add_row(*(str(row.get(col, "")) for col in ("orderId", "createTime", "updateTime", "side", "orderType", "vol", "dealVol", "dealAvgPrice", "usedMargin", "profit", "takerFee", "makerFee", "state")))
    console.print(table)

    if args.output:
        Path(args.output).write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
        console.print(f"Saved {len(rows)} rows to {args.output}")

    if not rows:
        console.print("[yellow]No rows returned. The current WEB session may not expose history that far back, the endpoint may differ for browser sessions, or the token may be stale.[/yellow]")


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only probe for old MEXC Futures order history using the current live WEB session")
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--start", required=True, help="Local Europe/Chisinau time, YYYY-MM-DD HH:MM:SS")
    parser.add_argument("--end", required=True, help="Local Europe/Chisinau time, YYYY-MM-DD HH:MM:SS")
    parser.add_argument("--show", type=int, default=20)
    parser.add_argument("--output", default="")
    args = parser.parse_args()
    try:
        asyncio.run(run(args))
    except (MexcWebError, ValueError) as exc:
        console.print(f"[red]LIVE HISTORY PROBE FAILED:[/red] {exc}")
        raise SystemExit(2) from exc


if __name__ == "__main__":
    main()
