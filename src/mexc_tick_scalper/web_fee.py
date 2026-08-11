from __future__ import annotations

from typing import Any

from .fees import SnapshotFeeProvider
from .models import FeeStatus
from .web_execution import MexcWebExecutionAdapter, MexcWebError


def _fee_rows(payload: Any) -> list[dict[str, Any]]:
    data = payload.get("data") if isinstance(payload, dict) else payload
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if isinstance(data, dict):
        # Support either one row or a symbol-keyed object without ever treating
        # missing rates as zero.
        if "symbol" in data:
            return [data]
        rows: list[dict[str, Any]] = []
        for symbol, value in data.items():
            if isinstance(value, dict):
                row = dict(value)
                row.setdefault("symbol", symbol)
                rows.append(row)
        return rows
    return []


def parse_fee_status(payload: Any, symbol: str) -> FeeStatus:
    wanted = symbol.upper()
    for item in _fee_rows(payload):
        if str(item.get("symbol", "")).upper() != wanted:
            continue
        maker = item.get("makerFeeRate")
        taker = item.get("takerFeeRate")
        if maker is None or taker is None:
            return FeeStatus(maker=None, taker=None, source="web_fee_missing_fields")
        return FeeStatus(maker=float(maker), taker=float(taker), source="web_account_fee_rate")
    return FeeStatus(maker=None, taker=None, source="web_fee_symbol_missing")


def provider_from_web_fee_payload(payload: Any) -> SnapshotFeeProvider:
    statuses: dict[str, FeeStatus] = {}
    for item in _fee_rows(payload):
        symbol = str(item.get("symbol", "")).upper()
        if not symbol:
            continue
        maker = item.get("makerFeeRate")
        taker = item.get("takerFeeRate")
        if maker is None or taker is None:
            statuses[symbol] = FeeStatus(None, None, "web_fee_missing_fields")
            continue
        statuses[symbol] = FeeStatus(float(maker), float(taker), "web_account_fee_rate")
    return SnapshotFeeProvider(statuses)


async def read_web_fee_status(adapter: MexcWebExecutionAdapter, symbol: str) -> FeeStatus:
    try:
        payload = await adapter.get_fee_rates()
    except MexcWebError:
        return FeeStatus(maker=None, taker=None, source="web_fee_request_failed")
    return parse_fee_status(payload, symbol)


async def read_web_fee_provider(adapter: MexcWebExecutionAdapter) -> SnapshotFeeProvider:
    try:
        payload = await adapter.get_fee_rates()
    except MexcWebError:
        return SnapshotFeeProvider({})
    return provider_from_web_fee_payload(payload)
