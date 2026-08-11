from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from .models import FeeStatus


class FeeProvider(Protocol):
    def status(self, symbol: str) -> FeeStatus: ...


@dataclass(slots=True)
class ConfiguredFeeProvider:
    """Conservative manual fallback; unknown symbols stay blocked."""

    verified_zero_fee_symbols: set[str]

    def status(self, symbol: str) -> FeeStatus:
        normalized = symbol.upper()
        if normalized in self.verified_zero_fee_symbols:
            return FeeStatus(maker=0.0, taker=0.0, source="configured_verified")
        return FeeStatus(maker=None, taker=None, source="unknown_blocked")


@dataclass(slots=True)
class SnapshotFeeProvider:
    """Immutable account-specific fee snapshot used by the scanner."""

    fees: dict[str, FeeStatus]

    def status(self, symbol: str) -> FeeStatus:
        return self.fees.get(
            symbol.upper(),
            FeeStatus(maker=None, taker=None, source="web_snapshot_missing"),
        )


def provider_from_config(cfg: dict) -> ConfiguredFeeProvider:
    raw = cfg.get("fees", {}).get("verified_zero_fee_symbols", []) or []
    return ConfiguredFeeProvider({str(x).upper() for x in raw})
