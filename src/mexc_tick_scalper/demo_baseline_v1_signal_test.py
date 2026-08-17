from __future__ import annotations

import argparse
import asyncio
from types import SimpleNamespace

from rich.console import Console

from . import prelive_100_trade_shadow as exact
from . import prelive_persistent_ioc_shadow_v2 as v2
from .lead_lag import fetch_binance_usdm_symbols, mexc_to_binance_symbol
from .live_zero_fee_universe import LiveZeroFeeContract
from .market import MexcPublicMarket

console = Console()
LIVE_REST = "https://contract.mexc.com"
LIVE_WS = "wss://contract.mexc.com/edge"


def _parse_symbols(raw: str) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in (raw or "").split(","):
        symbol = item.strip().upper()
        if symbol and symbol not in seen:
            seen.add(symbol)
            out.append(symbol)
    return out


async def _live_contracts(symbols: list[str]) -> list[LiveZeroFeeContract]:
    if not symbols:
        raise RuntimeError("no Demo test symbols supplied")
    binance = await fetch_binance_usdm_symbols()
    rows = await MexcPublicMarket(LIVE_REST, LIVE_WS).contracts()
    by_symbol = {str(r.get("symbol") or "").upper(): r for r in rows}
    out: list[LiveZeroFeeContract] = []
    for wanted in symbols:
        b_symbol = mexc_to_binance_symbol(wanted)
        row = by_symbol.get(wanted)
        if row is None or b_symbol not in binance:
            continue
        out.append(
            LiveZeroFeeContract(
                mexc_symbol=wanted,
                binance_symbol=b_symbol,
                max_leverage=int(row.get("maxLeverage") or 1),
                contract_size=float(row.get("contractSize") or 0),
                min_vol=float(row.get("minVol") or 0),
            )
        )
    if not out:
        raise RuntimeError("none of the Demo test symbols also exist on LIVE MEXC + Binance USD-M")
    return out


async def run(args: argparse.Namespace) -> None:
    symbols = _parse_symbols(args.demo_test_symbols)
    contracts = await _live_contracts(symbols)
    active_symbols = [c.mexc_symbol for c in contracts]

    original_discover = v2.discover_live_zero_fee_crosslisted
    original_build_profiles = v2.build_profiles
    original_select_profiles = v2.select_profiles
    original_fresh_zero = v2.FeeCache.fresh_zero

    async def selected_contracts_only():
        return contracts

    def test_profiles(_source):
        return []

    def selected_profiles(_profiles, **_kwargs):
        return [SimpleNamespace(symbol=symbol) for symbol in active_symbols]

    def fee_gate_disabled_for_demo_test(self, symbol, _now_ms):
        return symbol.upper() in set(active_symbols)

    v2.discover_live_zero_fee_crosslisted = selected_contracts_only
    v2.build_profiles = test_profiles
    v2.select_profiles = selected_profiles
    v2.FeeCache.fresh_zero = fee_gate_disabled_for_demo_test

    console.print(
        f"[bold cyan]DEMO TEST UNIVERSE[/bold cyan] {len(active_symbols)} cross-listed Testnet pair(s): "
        + ",".join(active_symbols)
    )
    console.print(
        "Only production pair-selection fee/profile gates are bypassed for this Demo execution test. "
        "The runner still chooses the strongest qualifying signal across the whole universe."
    )
    console.print(
        "All BASELINE_V1 signal, measured-RTT, retention, IOC, slippage, executable-cost, trailing and exit thresholds remain unchanged."
    )

    try:
        await exact.run(args)
    finally:
        v2.discover_live_zero_fee_crosslisted = original_discover
        v2.build_profiles = original_build_profiles
        v2.select_profiles = original_select_profiles
        v2.FeeCache.fresh_zero = original_fresh_zero


def build_parser() -> argparse.ArgumentParser:
    p = exact.build_parser()
    p.description = (
        "Frozen BASELINE_V1 thresholds across multiple Demo-testable Binance/MEXC symbols; "
        "production fee/profile eligibility bypassed only for execution validation"
    )
    p.add_argument("--demo-test-symbols", required=True, help="Comma-separated Testnet symbols")
    return p


def main() -> None:
    args = build_parser().parse_args()
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
