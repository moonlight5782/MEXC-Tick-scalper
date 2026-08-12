from __future__ import annotations

import asyncio
import time
import uuid

from rich.console import Console

from .demo_hybrid_test import _flatten_position
from .demo_smoke import _assert_demo_safety
from .execution import OrderSide, PositionSnapshot
from .web_execution import MexcWebError, MexcWebExecutionAdapter, WebExecutionConfig

console = Console()


async def list_open_demo_positions(adapter: MexcWebExecutionAdapter) -> list[PositionSnapshot]:
    """Return all currently open TESTNET positions, not just one selected symbol."""
    response = await adapter._request("GET", "/private/position/open_positions")  # noqa: SLF001
    data = response.get("data", []) if isinstance(response, dict) else []
    if isinstance(data, dict):
        data = [data]

    out: list[PositionSnapshot] = []
    for item in data or []:
        symbol = str(item.get("symbol", "")).upper()
        hold_vol = float(item.get("holdVol") or 0)
        if not symbol or hold_vol <= 0:
            continue
        try:
            qty = await adapter._from_contract_vol(symbol, hold_vol)  # noqa: SLF001
        except Exception as exc:  # keep cleanup explicit if one contract cannot be parsed
            raise MexcWebError(f"cannot decode open Demo position {symbol}: {exc}") from exc
        if qty <= 0:
            continue
        side = OrderSide.LONG if int(item.get("positionType") or 0) == 1 else OrderSide.SHORT
        out.append(
            PositionSnapshot(
                symbol=symbol,
                side=side,
                qty=qty,
                entry_price=float(item.get("holdAvgPrice") or item.get("openAvgPrice") or 0),
                leverage=int(item.get("leverage") or 1),
                isolated=int(item.get("openType") or 0) == 1,
                position_id=str(item.get("positionId")) if item.get("positionId") is not None else None,
                liquidation_price=float(item.get("liquidatePrice") or 0) or None,
                unrealized_pnl=None,
            )
        )
    return out


async def wait_account_flat(
    adapter: MexcWebExecutionAdapter,
    *,
    confirmations: int = 4,
    delay_seconds: float = 0.35,
    timeout_seconds: float = 8.0,
) -> bool:
    """Require several consecutive empty snapshots because testnet state can lag."""
    deadline = time.monotonic() + timeout_seconds
    empty_streak = 0
    while time.monotonic() < deadline:
        positions = await list_open_demo_positions(adapter)
        if not positions:
            empty_streak += 1
            if empty_streak >= confirmations:
                return True
        else:
            empty_streak = 0
        await asyncio.sleep(delay_seconds)
    return False


async def flatten_all_demo_positions(
    *,
    reason: str,
    max_passes: int = 5,
    quiet_if_flat: bool = False,
) -> None:
    """Close every open MEXC TESTNET position and confirm the account stays flat."""
    cfg = WebExecutionConfig.demo_from_env(write_enabled=True)
    _assert_demo_safety(cfg)

    async with MexcWebExecutionAdapter(cfg) as adapter:
        for pass_no in range(1, max_passes + 1):
            positions = await list_open_demo_positions(adapter)
            if not positions:
                if await wait_account_flat(adapter):
                    if not quiet_if_flat:
                        console.print(f"[green]DEMO FLAT CONFIRMED[/green] ({reason})")
                    return
                continue

            console.print(
                f"[yellow]DEMO CLEANUP[/yellow] ({reason}) pass={pass_no}: "
                f"closing {len(positions)} open position(s)"
            )
            for position in positions:
                console.print(
                    f"  closing {position.symbol} "
                    f"{'LONG' if position.side is OrderSide.LONG else 'SHORT'} qty={position.qty:g}"
                )
                await _flatten_position(adapter, position, f"{reason}:{position.symbol}:{uuid.uuid4().hex[:8]}")

            if await wait_account_flat(adapter):
                console.print(f"[green]DEMO FLAT CONFIRMED[/green] ({reason})")
                return
            await asyncio.sleep(0.5)

        residual = await list_open_demo_positions(adapter)
        if residual:
            detail = ", ".join(f"{p.symbol}:{p.qty:g}" for p in residual)
            raise MexcWebError(f"Demo cleanup failed after {max_passes} passes; residual positions: {detail}")
        raise MexcWebError("Demo cleanup could not confirm a stable flat account state")
