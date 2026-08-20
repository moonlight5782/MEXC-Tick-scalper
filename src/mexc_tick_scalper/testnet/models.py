from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from ..live_zero_fee_universe import LiveZeroFeeContract
from ..persistent_lag_profile import PairLagProfile


class FeeScope(Enum):
    ALL = "all"
    ZERO_ONLY = "zero_only"


@dataclass(frozen=True, slots=True)
class PublicContract:
    contract: LiveZeroFeeContract

    @property
    def symbol(self) -> str:
        return self.contract.mexc_symbol


@dataclass(frozen=True, slots=True)
class TestnetContract:
    contract: LiveZeroFeeContract
    demo_maker_fee: float | None
    demo_taker_fee: float | None
    demo_max_leverage: int
    metadata_rtt_ms: float

    @property
    def symbol(self) -> str:
        return self.contract.mexc_symbol


@dataclass(slots=True)
class ScanSignal:
    started_ms: int
    direction: int
    residual_bps: float
    strength: float
    trade_entry_seen: bool = False
    lifetime_ms: float | None = None
    terminal_reason: str = ""


@dataclass(slots=True)
class ScanStats:
    signals: list[ScanSignal] = field(default_factory=list)
    active: ScanSignal | None = None


@dataclass(frozen=True, slots=True)
class RankedCandidate:
    profile: PairLagProfile
    contract: LiveZeroFeeContract
    current_survival: float
    score: float


@dataclass(frozen=True, slots=True)
class CandidateView:
    candidate: RankedCandidate
    trade_entry_hits: int
    demo_maker_fee: float | None
    demo_taker_fee: float | None
    demo_max_leverage: int
    metadata_rtt_ms: float

    @property
    def symbol(self) -> str:
        return self.candidate.profile.symbol
