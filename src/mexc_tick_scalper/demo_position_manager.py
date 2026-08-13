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

def _position_already_closed_error(exc: MexcWebError) -> bool:
    text = str(exc)
    return "code=2009" in text and "Position is nonexistent or closed" in text


async def _confirm_symbol_absent(
    adapter: MexcWebExecutionAdapter,
    symbol: str,
    *,
    checks: int = 5,
    poll_seconds: float = 0.10,
) -> bool:
    for index in range(max(1, checks)):
        if await adapter.get_position(symbol) is not None:
            return False
        if index + 1 < checks:
            await asyncio.sleep(poll_seconds)
    return True


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
        except Exception as exc:
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
    stable_seconds: float = 3.0,
    poll_seconds: float = 0.35,
    timeout_seconds: float = 10.0,
) -> bool:
    """Require a continuously-flat window because TESTNET position state can appear late."""
    deadline = time.monotonic() + timeout_seconds
    flat_since: float | None = None
    while time.monotonic() < deadline:
        positions = await list_open_demo_positions(adapter)
        now = time.monotonic()
        if not positions:
            if flat_since is None:
                flat_since = now
            if now - flat_since >= stable_seconds:
                return True
        else:
            flat_since = None
        await asyncio.sleep(poll_seconds)
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
                try:
                    await _flatten_position(
                        adapter,
                        position,
                        f"{reason}:{position.symbol}:{uuid.uuid4().hex[:8]}",
                    )
                except MexcWebError as exc:
                    if _position_already_closed_error(exc) and await _confirm_symbol_absent(
                        adapter, position.symbol
                    ):
                        console.print(
                            f"  {position.symbol} already closed; stale TESTNET position snapshot ignored"
                        )
                        continue
                    raise

            if await wait_account_flat(adapter):
                console.print(f"[green]DEMO FLAT CONFIRMED[/green] ({reason})")
                return
            await asyncio.sleep(0.5)

        residual = await list_open_demo_positions(adapter)
        if residual:
            detail = ", ".join(f"{p.symbol}:{p.qty:g}" for p in residual)
            raise MexcWebError(f"Demo cleanup failed after {max_passes} passes; residual positions: {detail}")
        raise MexcWebError("Demo cleanup could not confirm a stable flat account state")
