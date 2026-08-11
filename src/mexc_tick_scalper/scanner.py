from __future__ import annotations

from dataclasses import dataclass
import aiohttp

from .fees import ConfiguredFeeProvider


@dataclass(slots=True)
class Candidate:
    symbol: str
    last: float
    bid: float
    ask: float
    volume24: float
    spread_bps: float


async def scan_candidates(rest_base_url: str, cfg: dict, fee_provider: ConfiguredFeeProvider) -> list[Candidate]:
    url = f"{rest_base_url.rstrip('/')}/api/v1/contract/ticker"
    async with aiohttp.ClientSession() as session:
        async with session.get(url, timeout=10) as resp:
            resp.raise_for_status()
            payload = await resp.json()

    rows = payload.get("data") or []
    if isinstance(rows, dict):
        rows = [rows]

    min_volume = float(cfg.get("scanner", {}).get("min_volume24", 1_000_000))
    max_spread_bps = float(cfg.get("scanner", {}).get("max_spread_bps", 12.0))

    out: list[Candidate] = []
    for row in rows:
        symbol = str(row.get("symbol") or "").upper()
        if not symbol or not fee_provider.status(symbol).zero_confirmed:
            continue
        bid = float(row.get("bid1") or 0)
        ask = float(row.get("ask1") or 0)
        last = float(row.get("lastPrice") or 0)
        volume24 = float(row.get("volume24") or 0)
        if bid <= 0 or ask <= 0 or last <= 0 or volume24 < min_volume:
            continue
        mid = (bid + ask) / 2
        spread_bps = (ask - bid) / mid * 10_000 if mid else 999999.0
        if spread_bps > max_spread_bps:
            continue
        out.append(Candidate(symbol, last, bid, ask, volume24, spread_bps))

    out.sort(key=lambda x: (x.volume24 / max(x.spread_bps, 0.01)), reverse=True)
    return out
