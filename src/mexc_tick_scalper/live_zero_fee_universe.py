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


def _load_project_env() -> None:
    """Load the repository-root .env without overriding explicit process env."""
    root = Path(__file__).resolve().parents[2]
    load_dotenv(root / ".env", override=False)


async def discover_live_zero_fee_crosslisted() -> list[LiveZeroFeeContract]:
    """Read the real account fee table without enabling any LIVE writes.

    The result is the strategy research universe: contracts that are currently
    listed on LIVE MEXC, have exact maker=0 and taker=0 for the logged-in web
    account, and have a matching Binance USD-M perpetual.
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
            )
        )

    out.sort(key=lambda item: item.mexc_symbol)
    return out
