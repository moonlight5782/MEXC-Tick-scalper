from __future__ import annotations

import argparse
import asyncio
import os
import uuid

from rich.console import Console

from .execution import OrderSide
from .web_execution import MexcWebError, MexcWebExecutionAdapter, WebExecutionConfig
from .web_fee import read_web_fee_status

console = Console()


def _assert_demo_safety(config: WebExecutionConfig) -> None:
    config.validate_environment()
    if config.environment != "demo":
        raise MexcWebError("refusing writes: adapter is not configured for Demo")
    if os.getenv("MEXC_DEMO_WRITE", "").strip().upper() != "YES":
        raise MexcWebError("refusing writes: set MEXC_DEMO_WRITE=YES for Demo only")


async def run(args: argparse.Namespace) -> None:
    cfg = WebExecutionConfig.demo_from_env(write_enabled=True)
    _assert_demo_safety(cfg)
    symbol = args.symbol.upper()
    side = OrderSide.LONG if args.side == "long" else OrderSide.SHORT

    async with MexcWebExecutionAdapter(cfg) as adapter:
        existing = await adapter.get_position(symbol)
        if existing is not None:
            raise MexcWebError(f"refusing smoke test: {symbol} already has an open position")

        fee = await read_web_fee_status(adapter, symbol)
        if not fee.zero_confirmed:
            raise MexcWebError(
                f"refusing smoke test: zero fee not confirmed for {symbol} "
                f"(maker={fee.maker}, taker={fee.taker}, source={fee.source})"
            )

        detail = await adapter.get_contract_detail(symbol)
        contract_size = float(detail.get("contractSize") or 0)
        min_vol = float(detail.get("minVol") or 0)
        max_leverage = int(detail.get("maxLeverage") or 1)
        if contract_size <= 0 or min_vol <= 0:
            raise MexcWebError("invalid contract sizing metadata")

        base_qty = contract_size * min_vol * max(1, int(args.min_vol_multiplier))
        leverage = min(max(1, int(args.leverage)), max_leverage)
        price = await adapter.get_best_price(symbol, side)

        entry_id = f"demo-entry-{uuid.uuid4().hex}"
        console.print(
            f"Demo IOC {side.value}: symbol={symbol} base_qty={base_qty:g} "
            f"price={price:g} leverage={leverage}x"
        )
        fill = await adapter.open_ioc(
            symbol=symbol,
            side=side,
            price=price,
            qty=base_qty,
            leverage=leverage,
            client_order_id=entry_id,
        )
        console.print(
            f"Entry fill: qty={fill.filled_qty:g}/{fill.requested_qty:g} "
            f"avg={fill.avg_price:g} fee={fill.fee_usdt:g}"
        )
        if fill.filled_qty <= 0:
            console.print("[yellow]IOC was not filled; nothing to close.[/yellow]")
            return
        if fill.fee_usdt != 0:
            raise MexcWebError(f"unexpected non-zero entry fee in Demo smoke test: {fill.fee_usdt}")

        close_side = OrderSide.SHORT if side is OrderSide.LONG else OrderSide.LONG
        exit_id = f"demo-exit-{uuid.uuid4().hex}"
        closed = await adapter.close_market_reduce_only(
            symbol=symbol,
            qty=fill.filled_qty,
            side=close_side,
            client_order_id=exit_id,
        )
        console.print(
            f"Exit fill: qty={closed.filled_qty:g} avg={closed.avg_price:g} fee={closed.fee_usdt:g}"
        )
        remaining = await adapter.get_position(symbol)
        if remaining is not None:
            raise MexcWebError(f"position still open after smoke close: qty={remaining.qty}")
        if closed.fee_usdt != 0:
            raise MexcWebError(f"unexpected non-zero exit fee in Demo smoke test: {closed.fee_usdt}")
        console.print("[green]DEMO EXECUTION SMOKE PASSED[/green]")


def main() -> None:
    parser = argparse.ArgumentParser(description="Guarded MEXC Demo WEB execution smoke test")
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--side", choices=["long", "short"], default="long")
    parser.add_argument("--leverage", type=int, default=5)
    parser.add_argument("--min-vol-multiplier", type=int, default=1)
    args = parser.parse_args()
    try:
        asyncio.run(run(args))
    except MexcWebError as exc:
        console.print(f"[red]DEMO SMOKE FAILED:[/red] {exc}")
        raise SystemExit(2) from exc


if __name__ == "__main__":
    main()
