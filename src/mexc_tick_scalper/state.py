from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .models import FeeStatus


class SymbolEligibility(str, Enum):
    ELIGIBLE = "eligible"
    PAUSED_FEE = "paused_fee"
    PAUSED_EDGE = "paused_edge"
    PAUSED_RISK = "paused_risk"
    COOLDOWN = "cooldown"
    UNKNOWN = "unknown"


@dataclass(slots=True)
class EligibilityState:
    symbol: str
    status: SymbolEligibility = SymbolEligibility.UNKNOWN
    reason: str = "not_checked"
    last_fee: FeeStatus | None = None
    changed_at_ms: int = 0

    @property
    def can_open_new_position(self) -> bool:
        return self.status is SymbolEligibility.ELIGIBLE


def apply_fee_status(state: EligibilityState, fee: FeeStatus, now_ms: int) -> EligibilityState:
    """Update eligibility from the latest fee check.

    Fee changes never permanently blacklist a symbol. A non-zero or unknown fee
    pauses only new entries. Once maker+taker are confirmed zero again, the
    symbol becomes eligible again (subject to later edge/risk gates).
    """
    state.last_fee = fee
    previous = state.status

    if fee.zero_confirmed:
        state.status = SymbolEligibility.ELIGIBLE
        state.reason = "maker_and_taker_zero_confirmed"
    elif fee.maker is None or fee.taker is None:
        state.status = SymbolEligibility.PAUSED_FEE
        state.reason = "fee_unknown"
    else:
        state.status = SymbolEligibility.PAUSED_FEE
        state.reason = f"non_zero_fee maker={fee.maker} taker={fee.taker}"

    if state.status != previous:
        state.changed_at_ms = now_ms
    return state
