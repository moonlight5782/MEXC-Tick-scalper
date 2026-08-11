from __future__ import annotations

from dataclasses import dataclass

from .models import FeeStatus


@dataclass(slots=True)
class ConfiguredFeeProvider:
    """Conservative fee provider.

    A symbol is zero-fee only when it is explicitly verified by a trusted
    account-specific source and placed in config. Unknown symbols stay blocked.
    This adapter is intentionally replaceable by a future web-session fee reader.
    """

    verified_zero_fee_symbols: set[str]

    def status(self, symbol: str) -> FeeStatus:
        normalized = symbol.upper()
        if normalized in self.verified_zero_fee_symbols:
            return FeeStatus(maker=0.0, taker=0.0, source="configured_verified")
        return FeeStatus(maker=None, taker=None, source="unknown_blocked")


def provider_from_config(cfg: dict) -> ConfiguredFeeProvider:
    raw = cfg.get("fees", {}).get("verified_zero_fee_symbols", []) or []
    return ConfiguredFeeProvider({str(x).upper() for x in raw})
