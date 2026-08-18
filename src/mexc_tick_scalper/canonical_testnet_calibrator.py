from __future__ import annotations

import argparse
import asyncio
import csv
import time
import uuid
from pathlib import Path

from rich.console import Console

from .canonical_env import load_project_env, require_demo_write_enabled
from .canonical_execution import CanonicalTestnetExecution
from .execution import OrderSide

console = Console()

FIELDS = [
    "ts_ms", "symbol", "side", "requested_notional_usdt", "requested_qty",
    "filled_qty", "entry_price", "entry_fee_usdt", "entry_post_to_confirm_ms",
    "entry_post_to_visible_ms", "exit_price", "exit_fee_usdt",
    "close_post_to_confirm_ms", "close_post_to_flat_ms", "roundtrip_pnl_usdt",
]


def _append(path: Path, row: dict[str, object]) -> None:
    exists = path.exists()
    with path.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        if not exists:
            writer.writeheader()
        writer.writerow({name: row.get(name, "") for name in FIELDS})


async def run(args: argparse.Namespace) -> None:
    symbol = args.symbol.upper()
    output = Path(args.output)
    console.print("[bold cyan]CANONICAL MEXC TESTNET EXECUTION CALIBRATOR[/bold cyan]")
    console.print("This measures execution mechanics only. It does NOT validate strategy PnL or LIVE alpha.")
    console.print(f"symbol={symbol} rounds={args.rounds} target=${args.notional_usdt:.0f} leverage<={args.leverage}x")

    async with CanonicalTestnetExecution(
        leverage=args.leverage,
        reconcile_timeout_s=args.reconcile_timeout_seconds,
        poll_ms=args.reconcile_poll_ms,
    ) as engine:
        await engine.preflight([symbol])
        for idx in range(args.rounds):
            side = OrderSide.LONG if idx % 2 == 0 else OrderSide.SHORT
            best = await engine.adapter.get_best_price(symbol, side)
            cross = args.ioc_cross_bps / 10_000.0
            price = best * (1.0 + cross if side is OrderSide.LONG else 1.0 - cross)
            qty = args.notional_usdt / best
            client = f"cal-e-{uuid.uuid4().hex}"[:32]
            started = time.time_ns() / 1_000_000.0
            opened = await engine.open_ioc_once(
                symbol=symbol,
                side=side,
                price=price,
                requested_qty=qty,
                client_order_id=client,
            )
            if opened.position is None or opened.fill.filled_qty <= 0:
                console.print(
                    f"ROUND {idx+1}/{args.rounds} {side.value.upper()} NOFILL "
                    f"entry_confirm={opened.timing.entry_post_to_confirm_ms:.1f}ms"
                )
                await asyncio.sleep(args.interval_seconds)
                continue

            close_fill, close_timing = await engine.close_known_position(
                client_order_id=f"cal-x-{uuid.uuid4().hex}"[:32]
            )
            signed = 1.0 if side is OrderSide.LONG else -1.0
            gross = signed * opened.fill.filled_qty * (close_fill.avg_price - opened.fill.avg_price)
            net = gross - opened.fill.fee_usdt - close_fill.fee_usdt
            row = {
                "ts_ms": int(started),
                "symbol": symbol,
                "side": side.value,
                "requested_notional_usdt": args.notional_usdt,
                "requested_qty": qty,
                "filled_qty": opened.fill.filled_qty,
                "entry_price": opened.fill.avg_price,
                "entry_fee_usdt": opened.fill.fee_usdt,
                "entry_post_to_confirm_ms": opened.timing.entry_post_to_confirm_ms,
                "entry_post_to_visible_ms": opened.timing.entry_post_to_visible_ms,
                "exit_price": close_fill.avg_price,
                "exit_fee_usdt": close_fill.fee_usdt,
                "close_post_to_confirm_ms": close_timing.close_post_to_confirm_ms,
                "close_post_to_flat_ms": close_timing.close_post_to_flat_ms,
                "roundtrip_pnl_usdt": net,
            }
            _append(output, row)
            console.print(
                f"ROUND {idx+1}/{args.rounds} {side.value.upper()} fill={opened.fill.filled_qty:g} "
                f"entry confirm/visible={opened.timing.entry_post_to_confirm_ms:.1f}/"
                f"{opened.timing.entry_post_to_visible_ms:.1f}ms close confirm/flat="
                f"{close_timing.close_post_to_confirm_ms:.1f}/{close_timing.close_post_to_flat_ms:.1f}ms "
                f"fees=${opened.fill.fee_usdt + close_fill.fee_usdt:.4f} net=${net:+.4f}"
            )
            await asyncio.sleep(args.interval_seconds)

    console.print(f"Telemetry CSV: {output.resolve()}")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Measure real MEXC Testnet IOC/open/close execution latency")
    p.add_argument("--symbol", default="BTC_USDT")
    p.add_argument("--rounds", type=int, default=5)
    p.add_argument("--notional-usdt", type=float, default=1000.0)
    p.add_argument("--leverage", type=int, default=10)
    p.add_argument("--ioc-cross-bps", type=float, default=1.0)
    p.add_argument("--interval-seconds", type=float, default=2.0)
    p.add_argument("--reconcile-timeout-seconds", type=float, default=2.0)
    p.add_argument("--reconcile-poll-ms", type=float, default=25.0)
    p.add_argument("--output", default="canonical_testnet_execution.csv")
    return p


def main() -> None:
    load_project_env()
    require_demo_write_enabled()
    args = build_parser().parse_args()
    if args.rounds <= 0 or args.notional_usdt <= 0 or args.ioc_cross_bps < 0:
        raise SystemExit("rounds/notional must be positive and ioc-cross-bps non-negative")
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
