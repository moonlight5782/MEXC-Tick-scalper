from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable

from .models import FeeStatus
from .state import EligibilityState, apply_fee_status


FeeFetcher = Callable[[str], FeeStatus | Awaitable[FeeStatus]]


async def _resolve(value: FeeStatus | Awaitable[FeeStatus]) -> FeeStatus:
    if isinstance(value, FeeStatus):
        return value
    return await value


class FeeMonitor:
    """Periodically refresh account-specific fees without permanent blacklists."""

    def __init__(self, fetcher: FeeFetcher, recheck_seconds: float = 30.0) -> None:
        if recheck_seconds <= 0:
            raise ValueError("recheck_seconds must be > 0")
        self.fetcher = fetcher
        self.recheck_seconds = recheck_seconds
        self.states: dict[str, EligibilityState] = {}

    async def check_once(self, symbol: str) -> EligibilityState:
        normalized = symbol.upper()
        state = self.states.setdefault(normalized, EligibilityState(normalized))
        try:
            fee = await _resolve(self.fetcher(normalized))
        except Exception as exc:  # fail closed; the next scheduled check can recover
            fee = FeeStatus(maker=None, taker=None, source=f"fee_check_error:{type(exc).__name__}")
        return apply_fee_status(state, fee, int(time.time() * 1000))

    async def watch(
        self,
        symbols: list[str],
        on_update: Callable[[EligibilityState], Awaitable[None] | None] | None = None,
    ) -> None:
        while True:
            for symbol in symbols:
                state = await self.check_once(symbol)
                if on_update is not None:
                    maybe = on_update(state)
                    if maybe is not None:
                        await maybe
            await asyncio.sleep(self.recheck_seconds)
