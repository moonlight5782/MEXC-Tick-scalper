from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

from .lead_lag import fetch_binance_usdm_symbols, mexc_to_binance_symbol
from .market import MexcPublicMarket
from .web_execution import MexcWebExecutionAdapter, WebExecutionConfig
from .web_fee import read_web_fee_provider

LIVE_REST = "https://contract.mexc.com"
LIVE_WS = "wss://contract.mexc.com/edge"


@dataclass(frozen=True, slots=True)
class LiveZeroFeeContract:
    mexc_symbol: str
    binance_symbol: str
    max_leverage: int
    contract_size: float
    min_vol: float
    maintenance_margin_rate: float = 0.0
    initial_margin_rate: float = 0.0
    risk_base_vol: float = 0.0
    risk_incr_vol: float = 0.0
    risk_incr_mmr: float = 0.0
    risk_level_limit: int = 1
    risk_limit_type: str = "BY_VOLUME"


def _load_project_env() -> None:
    """Load the repository-root .env without overriding explicit process env."""
    root = Path(__file__).resolve().parents[2]
    load_dotenv(root / ".env", override=False)


async def discover_live_zero_fee_crosslisted() -> list[LiveZeroFeeContract]:
    """Read the real account fee table without enabling any LIVE writes.

    The result is the strategy research universe: contracts that are currently
    listed on LIVE MEXC, have exact maker=0 and taker=0 for the logged-in web
    account, and have a matching Binance USD-M perpetual.  Public contract risk
    parameters are carried through so shadow positions can estimate isolated
    liquidation using the same current MMR/risk-tier inputs advertised by MEXC.
    """
    _load_project_env()

    binance_symbols = await fetch_binance_usdm_symbols()
    market = MexcPublicMarket(LIVE_REST, LIVE_WS)
    contracts = await market.contracts()

    cfg = WebExecutionConfig.from_env(write_enabled=False)
    async with MexcWebExecutionAdapter(cfg) as adapter:
        fees = await read_web_fee_provider(adapter)

    out: list[LiveZeroFeeContract] = []
    for row in contracts:
        symbol = str(row.get("symbol") or "").upper()
        if not symbol:
            continue
        b_symbol = mexc_to_binance_symbol(symbol)
        if b_symbol not in binance_symbols:
            continue
        status = fees.status(symbol)
        if status.maker != 0 or status.taker != 0:
            continue
        out.append(
            LiveZeroFeeContract(
                mexc_symbol=symbol,
                binance_symbol=b_symbol,
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
        )

    out.sort(key=lambda item: item.mexc_symbol)
    return out
