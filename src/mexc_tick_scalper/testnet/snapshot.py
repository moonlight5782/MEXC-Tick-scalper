from __future__ import annotations

from ..microspread import MicroSpreadModel


_INVALID_REASONS = {
    "warming_up",
    "warming_baseline",
    "warming_horizon",
    "stale_binance",
    "stale_mexc",
}


def event_key(model: MicroSpreadModel) -> tuple[int, int] | None:
    """Return the latest Binance/MEXC update pair used for duplicate-state detection."""
    if not model.binance or not model.mexc:
        return None
    return int(model.binance[-1][0]), int(model.mexc[-1][0])


def valid_snapshot(snapshot) -> bool:
    """Reject warm-up/stale snapshots while preserving the frozen strategy semantics."""
    return snapshot.reason not in _INVALID_REASONS
