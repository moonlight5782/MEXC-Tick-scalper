from __future__ import annotations

import asyncio
import os
import sys

from . import demo_hybrid_test as hybrid
from .demo_smoke import _assert_demo_safety
from .execution import OrderSide
from .market import MexcPublicMarket
from .web_execution import MexcWebError, MexcWebExecutionAdapter, WebExecutionConfig

LIVE_REST = "https://contract.mexc.com"
LIVE_WS = "wss://contract.mexc.com/edge"
DEFAULT_MAX_DIVERGENCE_BPS = 25.0
AUTO_FLATTEN_FLAG = "--auto-flatten-start"


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
        self._max_divergence_bps = float(
            os.getenv("MEXC_DEMO_LIVE_MAX_DIVERGENCE_BPS", str(DEFAULT_MAX_DIVERGENCE_BPS))
        )
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
        divergence_bps = abs(demo_price - live_mid) / live_mid * 10_000.0
        self.last_divergence_bps = divergence_bps
        if divergence_bps > self._max_divergence_bps:
            raise MexcWebError(
                f"LIVE/DEMO price divergence too large for {symbol}: "
                f"{divergence_bps:.2f}bps > {self._max_divergence_bps:.2f}bps"
            )
        return demo_price


def _arg_value(name: str) -> str | None:
    try:
        idx = sys.argv.index(name)
    except ValueError:
        return None
    if idx + 1 >= len(sys.argv):
        return None
    return sys.argv[idx + 1]


async def _auto_flatten_demo_start(symbol: str) -> None:
    """Flatten only an existing TESTNET position before the experiment starts."""
    hybrid._load_project_env()
    cfg = WebExecutionConfig.demo_from_env(write_enabled=True)
    _assert_demo_safety(cfg)
    async with _GuardedDemoAdapter(cfg) as adapter:
        existing = await adapter.get_position(symbol)
        if existing is None:
            hybrid.console.print(f"[green]START FLATTEN[/green]: no existing Demo position for {symbol}")
            return
        hybrid.console.print(
            f"[yellow]START FLATTEN[/yellow]: closing existing Demo {symbol} "
            f"{'LONG' if existing.side is OrderSide.LONG else 'SHORT'} qty={existing.qty:g}"
        )
        fill = await hybrid._flatten_position(adapter, existing, "startup_auto_flatten")
        residual = await adapter.get_position(symbol)
        if residual is not None:
            raise MexcWebError(f"startup auto-flatten failed; residual qty={residual.qty}")
        hybrid.console.print(
            f"[green]START FLATTEN COMPLETE[/green]: qty={fill.filled_qty:g} avg={fill.avg_price:g} fee={fill.fee_usdt:g}"
        )


def main() -> None:
    auto_flatten = AUTO_FLATTEN_FLAG in sys.argv
    if auto_flatten:
        sys.argv.remove(AUTO_FLATTEN_FLAG)

    symbol = (_arg_value("--symbol") or "").upper()
    if auto_flatten:
        if not symbol:
            raise SystemExit("--auto-flatten-start requires --symbol")
        try:
            asyncio.run(_auto_flatten_demo_start(symbol))
        except MexcWebError as exc:
            hybrid.console.print(f"[red]LIVE-SIGNAL DEMO FAILED:[/red] {exc}")
            raise SystemExit(2) from exc

    # Patch only this process. Hybrid still builds Demo WebExecutionConfig, whose
    # hard environment guard rejects any non-testnet private/write host.
    hybrid.MexcPublicMarket = _LiveSignalMarket
    hybrid.MexcWebExecutionAdapter = _GuardedDemoAdapter
    hybrid.console.print(
        "[cyan]LIVE SIGNAL / DEMO EXECUTION MODE[/cyan]: signal ticks=LIVE MEXC, "
        "orders/positions=TESTNET only, divergence guard enabled"
    )
    hybrid.console.print(
        "[dim]Note: the inherited scanner line may still say TESTNET trade ticks; "
        "in this mode the signal tape is LIVE.[/dim]"
    )
    hybrid.main()


if __name__ == "__main__":
    main()
