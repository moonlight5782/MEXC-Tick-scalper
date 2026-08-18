from __future__ import annotations

import asyncio
from dataclasses import dataclass

from rich.console import Console

from . import testnet_latency_arb_product as product
from .demo_hybrid_test import _load_project_env
from .demo_smoke import _assert_demo_safety
from .execution import OrderFill, OrderSide
from .lead_lag import fetch_binance_usdm_symbols, mexc_to_binance_symbol
from .testnet_known_good_v1 import _testnet_contract_rows
from .web_execution import MexcWebError, MexcWebExecutionAdapter, WebExecutionConfig

console = Console()
_ORIGINAL_OPEN_IOC = MexcWebExecutionAdapter.open_ioc
_ORIGINAL_RECONCILE = product._reconcile_ioc_position
_RISK_CAPS: dict[tuple[str, int], "RiskCap"] = {}
_DISABLED: set[tuple[str, int]] = set()
_REQUESTED_LEVERAGE = 10


@dataclass(frozen=True, slots=True)
class RiskCap:
    symbol: str
    position_type: int
    max_vol: float
    max_leverage: int
    mmr: float
    imr: float


def _position_type(side: OrderSide) -> int:
    return 1 if side is OrderSide.LONG else 2


def _parse_risk_limits(response: object) -> dict[tuple[str, int], RiskCap]:
    if not isinstance(response, dict):
        return {}
    data = response.get("data", {})
    rows: list[dict] = []
    if isinstance(data, dict):
        for value in data.values():
            if isinstance(value, list):
                rows.extend(x for x in value if isinstance(x, dict))
            elif isinstance(value, dict):
                rows.append(value)
    elif isinstance(data, list):
        rows = [x for x in data if isinstance(x, dict)]

    out: dict[tuple[str, int], RiskCap] = {}
    for row in rows:
        symbol = str(row.get("symbol") or "").upper()
        try:
            ptype = int(row.get("positionType") or 0)
            max_vol = float(row.get("maxVol") or 0)
            max_lev = int(row.get("maxLeverage") or 0)
            mmr = float(row.get("mmr") or 0)
            imr = float(row.get("imr") or 0)
        except (TypeError, ValueError):
            continue
        if symbol and ptype in (1, 2):
            out[(symbol, ptype)] = RiskCap(symbol, ptype, max_vol, max_lev, mmr, imr)
    return out


async def _read_all_risk_limits(adapter: MexcWebExecutionAdapter) -> dict[tuple[str, int], RiskCap]:
    response = await adapter._request("GET", "/private/account/risk_limit")
    return _parse_risk_limits(response)


async def _preflight_account_risk() -> None:
    """Initialize Testnet leverage before the strategy starts.

    This work is intentionally outside the signal->IOC path. A candidate is never delayed
    by leverage setup or risk-limit discovery. The exchange keeps leverage configuration
    per symbol/direction, so the setup persists for the trading session/account.
    """
    global _RISK_CAPS
    _load_project_env()
    cfg = WebExecutionConfig.demo_from_env(write_enabled=True)
    _assert_demo_safety(cfg)

    async with MexcWebExecutionAdapter(cfg) as adapter:
        if await adapter.get_positions():
            raise MexcWebError("Risk preflight refuses to alter leverage while a Testnet position is open")

        rows = await _testnet_contract_rows(adapter)
        binance = await fetch_binance_usdm_symbols()
        eligible = [
            symbol for symbol in rows
            if mexc_to_binance_symbol(symbol) in binance
        ]
        risk_before = await _read_all_risk_limits(adapter)

        configured = 0
        unavailable = 0
        for symbol in eligible:
            detail = rows[symbol]
            contract_max = max(1, int(detail.get("maxLeverage") or 1))
            for ptype in (1, 2):
                cap = risk_before.get((symbol, ptype))
                risk_max = cap.max_leverage if cap and cap.max_leverage > 0 else contract_max
                leverage = min(_REQUESTED_LEVERAGE, contract_max, risk_max)
                if leverage <= 0:
                    unavailable += 1
                    continue
                try:
                    await adapter._request(
                        "POST",
                        "/private/position/change_leverage",
                        payload={
                            "openType": 1,
                            "leverage": leverage,
                            "symbol": symbol,
                            "positionType": ptype,
                        },
                    )
                    configured += 1
                except MexcWebError as exc:
                    # Some Testnet contracts expose a book while trading/risk setup is disabled.
                    # Do not kill the product because one side of one contract is unusable.
                    console.print(
                        f"[yellow]RISK PREFLIGHT SKIP[/yellow] {symbol} "
                        f"{'LONG' if ptype == 1 else 'SHORT'}: {exc}"
                    )
                    unavailable += 1
                # Official private limit is 20 calls / 2 seconds. Stay below it.
                await asyncio.sleep(0.11)

        _RISK_CAPS = await _read_all_risk_limits(adapter)
        positive = sum(1 for key, cap in _RISK_CAPS.items() if key[0] in eligible and cap.max_vol > 0)
        zero = sum(1 for key, cap in _RISK_CAPS.items() if key[0] in eligible and cap.max_vol <= 0)
        console.print(
            f"[bold cyan]TESTNET PRIVATE RISK PREFLIGHT[/bold cyan] symbols={len(eligible)} "
            f"configured_sides={configured} positive_capacity={positive} zero_capacity={zero} setup_skips={unavailable}"
        )


async def _safe_open_ioc(self: MexcWebExecutionAdapter, **kwargs) -> OrderFill:
    """Submit exactly one IOC, sized by the account's private risk limit.

    Public contract.detail maxVol is not an account capacity. The private risk-limit row
    is authoritative for max contracts on the requested long/short side. A zero-capacity
    side becomes a no-fill/skip and cannot terminate the whole trading process.
    """
    symbol = str(kwargs["symbol"]).upper()
    side: OrderSide = kwargs["side"]
    ptype = _position_type(side)
    key = (symbol, ptype)
    requested_qty = float(kwargs["qty"])
    client_order_id = str(kwargs["client_order_id"])

    if key in _DISABLED:
        return OrderFill(symbol, side, requested_qty, 0.0, 0.0, 0.0, "RISK_DISABLED", client_order_id)

    cap = _RISK_CAPS.get(key)
    if cap is None or cap.max_vol <= 0 or cap.max_leverage <= 0:
        _DISABLED.add(key)
        console.print(
            f"[yellow]EXECUTION SKIP[/yellow] {symbol} {side.value.upper()} "
            "private_risk_capacity=0"
        )
        return OrderFill(symbol, side, requested_qty, 0.0, 0.0, 0.0, "RISK_CAPACITY_ZERO", client_order_id)

    detail = await self.get_contract_detail(symbol)
    contract_size = float(detail.get("contractSize") or 0)
    if contract_size <= 0:
        _DISABLED.add(key)
        return OrderFill(symbol, side, requested_qty, 0.0, 0.0, 0.0, "RISK_BAD_CONTRACT", client_order_id)

    max_base_qty = cap.max_vol * contract_size
    qty = min(requested_qty, max_base_qty)
    min_vol = float(detail.get("minVol") or 0)
    min_base_qty = min_vol * contract_size
    if qty + 1e-12 < min_base_qty:
        _DISABLED.add(key)
        console.print(
            f"[yellow]EXECUTION SKIP[/yellow] {symbol} {side.value.upper()} "
            f"risk_maxVol={cap.max_vol:g} below exchange minVol={min_vol:g}"
        )
        return OrderFill(symbol, side, requested_qty, 0.0, 0.0, 0.0, "RISK_BELOW_MIN", client_order_id)

    kwargs["qty"] = qty
    kwargs["leverage"] = min(max(1, int(kwargs["leverage"])), cap.max_leverage)

    try:
        # Exactly one marketable IOC attempt. Never chase/top-up after any fill.
        return await _ORIGINAL_OPEN_IOC(self, **kwargs)
    except MexcWebError as exc:
        text = str(exc)
        if "code=8819" not in text and "maximum number of contracts" not in text.lower():
            raise
        # The exchange rejected the IOC before a position was created. Disable that side for
        # this run instead of crashing or retrying the same signal at another leverage.
        _DISABLED.add(key)
        console.print(
            f"[yellow]EXECUTION CAPACITY REJECT[/yellow] {symbol} {side.value.upper()} "
            f"leverage={kwargs['leverage']}x private_maxVol={cap.max_vol:g}; side disabled for this run"
        )
        return OrderFill(symbol, side, requested_qty, 0.0, 0.0, 0.0, "RISK_8819", client_order_id)


async def _fast_reconcile(adapter, symbol, side, fill):
    if str(fill.order_id).startswith("RISK_"):
        return None
    return await _ORIGINAL_RECONCILE(adapter, symbol, side, fill)


def main() -> None:
    MexcWebExecutionAdapter.open_ioc = _safe_open_ioc
    product._reconcile_ioc_position = _fast_reconcile
    try:
        asyncio.run(_preflight_account_risk())
    except MexcWebError as exc:
        console.print(f"[red]TESTNET RISK PREFLIGHT FAILED:[/red] {exc}")
        raise SystemExit(2) from exc
    product.main()


if __name__ == "__main__":
    main()
