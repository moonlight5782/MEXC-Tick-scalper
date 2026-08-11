from __future__ import annotations

from typing import Any

from .models import FeeStatus
from .web_execution import MexcWebExecutionAdapter, MexcWebError


def parse_fee_status(payload: Any, symbol: str) -> FeeStatus:
    data = payload.get("data") if isinstance(payload, dict) else payload
    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list):
        return FeeStatus(maker=None, taker=None, source="web_fee_unknown")
    wanted = symbol.upper()
    for item in data:
        if not isinstance(item, dict):
            continue
        if str(item.get("symbol", "")).upper() != wanted:
            continue
        maker = item.get("makerFeeRate")
        taker = item.get("takerFeeRate")
        if maker is None or taker is None:
            return FeeStatus(maker=None, taker=None, source="web_fee_missing_fields")
        return FeeStatus(maker=float(maker), taker=float(taker), source="web_account_fee_rate")
    return FeeStatus(maker=None, taker=None, source="web_fee_symbol_missing")


async def read_web_fee_status(adapter: MexcWebExecutionAdapter, symbol: str) -> FeeStatus:
    try:
        payload = await adapter.get_fee_rates()
    except MexcWebError:
        return FeeStatus(maker=None, taker=None, source="web_fee_request_failed")
    return parse_fee_status(payload, symbol)
