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


async def _live_contract(symbol: str) -> LiveZeroFeeContract:
    wanted = symbol.upper()
    binance = await fetch_binance_usdm_symbols()
    b_symbol = mexc_to_binance_symbol(wanted)
    if b_symbol not in binance:
        raise RuntimeError(f"{wanted} has no Binance USD-M leader")

    market = MexcPublicMarket(LIVE_REST, LIVE_WS)
    rows = await market.contracts()
    row = next((r for r in rows if str(r.get("symbol") or "").upper() == wanted), None)
    if row is None:
        raise RuntimeError(f"{wanted} does not exist on LIVE MEXC, cannot compute the same Binance->MEXC signal")
    return LiveZeroFeeContract(
        mexc_symbol=wanted,
        binance_symbol=b_symbol,
        max_leverage=int(row.get("maxLeverage") or 1),
        contract_size=float(row.get("contractSize") or 0),
        min_vol=float(row.get("minVol") or 0),
    )


async def run(args: argparse.Namespace) -> None:
    symbol = args.demo_test_symbol.upper()
    contract = await _live_contract(symbol)

    original_discover = v2.discover_live_zero_fee_crosslisted
    original_build_profiles = v2.build_profiles
    original_select_profiles = v2.select_profiles
    original_fresh_zero = v2.FeeCache.fresh_zero

    async def selected_contract_only():
        return [contract]

    def test_profiles(_source):
        return []

    def selected_profile_only(_profiles, **_kwargs):
        return [SimpleNamespace(symbol=symbol)]

    def fee_gate_disabled_for_demo_test(self, _symbol, _now_ms):
        return True

    v2.discover_live_zero_fee_crosslisted = selected_contract_only
    v2.build_profiles = test_profiles
    v2.select_profiles = selected_profile_only
    v2.FeeCache.fresh_zero = fee_gate_disabled_for_demo_test

    console.print(
        f"[bold cyan]DEMO TEST SYMBOL {symbol}[/bold cyan] — pair-selection fee/profile gates are bypassed ONLY for this Demo test."
    )
    console.print(
        "All BASELINE_V1 signal, RTT, retention, IOC, slippage, executable-cost and exit thresholds remain unchanged."
    )
    console.print(
        "The Demo account pays its real entry+exit fees; those fees are handled by the parent Testnet mirror."
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
    p.description = "Frozen BASELINE_V1 thresholds on one Demo-testable Binance/MEXC symbol; production fee/profile eligibility bypassed"
    p.add_argument("--demo-test-symbol", required=True)
    return p


def main() -> None:
    args = build_parser().parse_args()
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
