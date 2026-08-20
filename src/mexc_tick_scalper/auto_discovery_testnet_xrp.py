from __future__ import annotations

import asyncio
from dataclasses import dataclass

from . import auto_discovery_testnet as testnet
from .lead_lag import fetch_binance_usdm_symbols, mexc_to_binance_symbol
from .live_zero_fee_universe import LIVE_REST, LIVE_WS, LiveZeroFeeContract, _load_project_env
from .market import MexcPublicMarket


SYMBOL = "XRP_USDT"


@dataclass(frozen=True, slots=True)
class _Profile:
    symbol: str


@dataclass(frozen=True, slots=True)
class _Candidate:
    profile: _Profile
    contract: LiveZeroFeeContract


async def _xrp_only_discover(args):
    del args
    binance_symbols = await fetch_binance_usdm_symbols()
    binance_symbol = mexc_to_binance_symbol(SYMBOL)
    if binance_symbol not in binance_symbols:
        raise RuntimeError(f"{SYMBOL} has no Binance USD-M counterpart {binance_symbol}")

    market = MexcPublicMarket(LIVE_REST, LIVE_WS)
    contracts = await market.contracts()
    row = next((item for item in contracts if str(item.get("symbol") or "").upper() == SYMBOL), None)
    if row is None:
        raise RuntimeError(f"{SYMBOL} is not currently listed on LIVE MEXC Futures")

    contract = LiveZeroFeeContract(
        mexc_symbol=SYMBOL,
        binance_symbol=binance_symbol,
        max_leverage=int(row.get("maxLeverage") or 1),
        contract_size=float(row.get("contractSize") or 0),
        min_vol=float(row.get("minVol") or 0),
        maintenance_margin_rate=float(row.get("maintenanceMarginRate") or 0),
        initial_margin_rate=float(row.get("initialMarginRate") or 0),
        risk_base_vol=float(row.get("riskBaseVol") or 0),
        risk_incr_vol=float(row.get("riskIncrVol") or 0),
        risk_incr_mmr=float(row.get("riskIncrMmr") or 0),
        risk_level_limit=max(1, int(row.get("riskLevelLimit") or 1)),
        risk_limit_type=str(row.get("riskLimitType") or "BY_VOLUME").upper(),
    )
    if contract.contract_size <= 0:
        raise RuntimeError(f"{SYMBOL} returned invalid LIVE contractSize={contract.contract_size}")

    testnet.console.print(
        "[bold yellow]TEMPORARY XRP TESTNET OVERRIDE[/bold yellow] "
        "AUTO pair selection / LIVE zero-fee eligibility is bypassed ONLY for this Testnet plumbing run."
    )
    testnet.console.print(
        f"Forced symbol={SYMBOL} Binance={binance_symbol} LIVE max_leverage={contract.max_leverage}x; "
        "8bps/3x signal, arrival economics, IOC/slippage/cost, sizing and exit/profit-runner logic remain unchanged."
    )
    return [_Candidate(_Profile(SYMBOL), contract)]


async def run(args):
    # XRP override bypasses the normal AUTO discovery path which normally loads
    # the repository-root .env. Load it explicitly before Demo config is built.
    _load_project_env()
    original_discover = testnet.auto.discover
    testnet.auto.discover = _xrp_only_discover
    try:
        return await testnet.run(args)
    finally:
        testnet.auto.discover = original_discover


def main() -> None:
    args = testnet.build_parser().parse_args()
    testnet.auto.apply_baseline_v1(args)
    if args.profit_runner_arm_bps < 0 or args.target_closed_trades <= 0:
        raise SystemExit("invalid profit-runner/trade limit")
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        testnet.console.print("\n[yellow]XRP Testnet stop requested.[/yellow]")
        raise SystemExit(130)
    except Exception as exc:
        testnet.console.print(f"[red]XRP TESTNET RUNNER STOPPED:[/red] {type(exc).__name__}: {exc}")
        raise SystemExit(2)


if __name__ == "__main__":
    main()
