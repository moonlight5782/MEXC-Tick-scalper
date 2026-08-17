from __future__ import annotations

import asyncio

from rich.console import Console
from rich.table import Table

from .demo_activity import sample_many
from .demo_discovery import _fetch_contracts
from .execution import OrderSide
from .web_execution import MexcWebError, MexcWebExecutionAdapter, WebExecutionConfig
from .web_fee import provider_from_web_fee_payload

console = Console()


async def run(sample_seconds: float = 6.0, limit: int = 20) -> None:
    cfg = WebExecutionConfig.demo_from_env(write_enabled=False)
    async with MexcWebExecutionAdapter(cfg) as adapter:
        contracts = await _fetch_contracts(adapter)
        fee_payload = await adapter.get_fee_rates()
        fees = provider_from_web_fee_payload(fee_payload)

        candidates: list[dict] = []
        for row in contracts:
            symbol = str(row.get("symbol") or "").upper()
            if not symbol:
                continue
            try:
                ask, bid = await asyncio.gather(
                    adapter.get_best_price(symbol, OrderSide.LONG),
                    adapter.get_best_price(symbol, OrderSide.SHORT),
                )
            except MexcWebError:
                continue
            if ask <= 0 or bid <= 0 or ask < bid:
                continue
            mid = (ask + bid) / 2.0
            fee = fees.status(symbol)
            candidates.append({
                "symbol": symbol,
                "spread_bps": (ask - bid) / mid * 10_000.0 if mid > 0 else 99999.0,
                "max_leverage": int(row.get("maxLeverage") or 1),
                "maker": fee.maker,
                "taker": fee.taker,
            })

    if not candidates:
        raise MexcWebError("no Demo contracts have a usable bid/ask book")

    console.print(f"Measuring TESTNET activity for {len(candidates)} contracts regardless of fee...")
    activity = await sample_many([x["symbol"] for x in candidates], seconds=sample_seconds)
    for row in candidates:
        s = activity.get(row["symbol"])
        row["trade_changes"] = s.change_rate if s else 0.0
        row["book_changes"] = s.book_change_rate if s else 0.0
        row["trades"] = s.trade_rate if s else 0.0
        row["activity"] = s.activity_rate if s else 0.0

    candidates.sort(
        key=lambda x: (
            -float(x["activity"]),
            -float(x["trade_changes"]),
            -float(x["book_changes"]),
            float(x["spread_bps"]),
        )
    )
    rows = candidates[: max(1, int(limit))]

    table = Table(title="MEXC Demo active contracts (fees allowed)")
    table.add_column("#", justify="right")
    table.add_column("Symbol")
    table.add_column("Activity/s", justify="right")
    table.add_column("Trade chg/s", justify="right")
    table.add_column("Book chg/s", justify="right")
    table.add_column("Spread bps", justify="right")
    table.add_column("Maker", justify="right")
    table.add_column("Taker", justify="right")
    table.add_column("Max lev", justify="right")
    for i, row in enumerate(rows, 1):
        table.add_row(
            str(i), row["symbol"], f"{row['activity']:.2f}", f"{row['trade_changes']:.2f}",
            f"{row['book_changes']:.2f}", f"{row['spread_bps']:.2f}",
            "?" if row["maker"] is None else str(row["maker"]),
            "?" if row["taker"] is None else str(row["taker"]),
            str(row["max_leverage"]),
        )
    console.print(table)
    console.print("Pick a genuinely active symbol; execution validation will subtract both entry and exit fees.")


def main() -> None:
    import argparse

    p = argparse.ArgumentParser(description="Rank active MEXC Demo contracts regardless of fee")
    p.add_argument("--sample-seconds", type=float, default=6.0)
    p.add_argument("--limit", type=int, default=20)
    args = p.parse_args()
    try:
        asyncio.run(run(args.sample_seconds, args.limit))
    except MexcWebError as exc:
        console.print(f"[red]DEMO ACTIVE-PAIR SCAN FAILED:[/red] {exc}")
        raise SystemExit(2) from exc


if __name__ == "__main__":
    main()
