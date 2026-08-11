from __future__ import annotations

import asyncio
import os

from . import demo_hybrid_test as hybrid
from .execution import OrderSide
from .market import MexcPublicMarket
from .web_execution import MexcWebError, MexcWebExecutionAdapter

LIVE_REST = "https://contract.mexc.com"
LIVE_WS = "wss://contract.mexc.com/edge"
DEFAULT_MAX_DIVERGENCE_BPS = 25.0


class _LiveSignalMarket:
    """Force Hybrid's signal/tape source to LIVE MEXC while leaving execution Demo-only."""

    def __new__(cls, *_args, **_kwargs):
        return MexcPublicMarket(LIVE_REST, LIVE_WS)


class _GuardedDemoAdapter(MexcWebExecutionAdapter):
    """Demo execution adapter with a LIVE-vs-Demo price sanity guard.

    The inherited config validation still requires futures.testnet.mexc.com for all
    private/write calls. Only public LIVE ticker data is consulted for the guard.
    """

    def __init__(self, config):
        super().__init__(config)
        self._live_market = MexcPublicMarket(LIVE_REST, LIVE_WS)
        self._max_divergence_bps = float(os.getenv("MEXC_DEMO_LIVE_MAX_DIVERGENCE_BPS", str(DEFAULT_MAX_DIVERGENCE_BPS)))
        self.last_divergence_bps: float | None = None

    async def get_best_price(self, symbol: str, side: OrderSide) -> float:
        demo_price = await super().get_best_price(symbol, side)
        live = await self._live_market.ticker(symbol)
        if live is None:
            raise MexcWebError(f"LIVE ticker unavailable for {symbol}")

        live_bid = float(live.bid or 0)
        live_ask = float(live.ask or 0)
        if live_bid <= 0 or live_ask <= 0:
            raise MexcWebError(f"LIVE bid/ask unavailable for {symbol}")

        live_mid = (live_bid + live_ask) / 2.0
        # Compare the corresponding Demo best side to LIVE mid. This is deliberately
        # conservative: if testnet drifts away from the real market, block the entry.
        divergence_bps = abs(demo_price - live_mid) / live_mid * 10_000.0
        self.last_divergence_bps = divergence_bps
        if divergence_bps > self._max_divergence_bps:
            raise MexcWebError(
                f"LIVE/DEMO price divergence too large for {symbol}: "
                f"{divergence_bps:.2f}bps > {self._max_divergence_bps:.2f}bps"
            )
        return demo_price


def main() -> None:
    # Patch only this process. Hybrid still builds Demo WebExecutionConfig, whose
    # hard environment guard rejects any non-testnet private/write host.
    hybrid.MexcPublicMarket = _LiveSignalMarket
    hybrid.MexcWebExecutionAdapter = _GuardedDemoAdapter
    hybrid.console.print(
        "[cyan]LIVE SIGNAL / DEMO EXECUTION MODE[/cyan]: market ticks=LIVE MEXC, "
        "orders/positions=TESTNET only, divergence guard enabled"
    )
    hybrid.main()


if __name__ == "__main__":
    main()
