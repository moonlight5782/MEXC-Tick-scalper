from __future__ import annotations

import asyncio
import os
import sys
import time

import aiohttp

from . import demo_hybrid_test as hybrid
from .demo_smoke import _assert_demo_safety
from .execution import OrderFill, OrderSide
from .market import MexcPublicMarket
from .orderbook_signal import EntryBookDecision, analyze_order_book, book_confirmation
from .web_execution import MexcWebError, MexcWebExecutionAdapter, WebExecutionConfig

LIVE_REST = "https://contract.mexc.com"
LIVE_WS = "wss://contract.mexc.com/edge"
DEFAULT_MAX_DIVERGENCE_BPS = 25.0
DEFAULT_BOOK_VETO_CONFIDENCE = 0.30
DEFAULT_BOOK_CONFIRM_CONFIDENCE = 0.20
DEFAULT_MIN_MOMENTUM_BPS = 0.20
DEFAULT_REQUIRE_BOOK_CONFIRMATION = True
AUTO_FLATTEN_FLAG = "--auto-flatten-start"
AUTO_FLATTEN_ENV = "MEXC_DEMO_AUTO_FLATTEN_START"
CLOSE_POSITION_RETRIES = 5
CLOSE_POSITION_RETRY_SECONDS = 0.12
DEMO_SPREAD_CACHE_SECONDS = 1.0
LIVE_TICKER_CACHE_SECONDS = 0.20
IOC_POSITION_VISIBLE_SECONDS = 4.0
IOC_POSITION_VISIBLE_POLL_SECONDS = 0.08
FAST_POSITION_WATCHDOG_SECONDS = 0.10
_LAST_DEMO_SPREAD_BPS: float | None = None


def _env_yes(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().upper() in {"1", "TRUE", "YES", "ON"}


def _strict_book_allows(decision: EntryBookDecision, require_confirmation: bool = True) -> bool:
    if not decision.allowed:
        return False
    if require_confirmation:
        return decision.reason == "book_confirmed"
    return True


def _as_pending_fill(fill: OrderFill) -> OrderFill:
    """Preserve order metadata but force Hybrid into its non-fatal zero-fill path."""
    return OrderFill(
        symbol=fill.symbol,
        side=fill.side,
        requested_qty=fill.requested_qty,
        filled_qty=0.0,
        avg_price=fill.avg_price,
        fee_usdt=fill.fee_usdt,
        order_id=fill.order_id,
        client_order_id=fill.client_order_id,
        position_id=fill.position_id,
    )


class _MomentumAlignedSignal(hybrid.MicrostructureSignal):
    """Reject trade-flow directions that disagree with actual price momentum.

    CVD/flow are still useful, but they are not allowed to overpower a price move
    in the opposite direction. This specifically removes the losing pattern seen
    in Demo where SHORT was opened with positive momentum or LONG with negative
    momentum.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._min_momentum_bps = max(
            0.0,
            float(os.getenv("MEXC_MIN_ALIGNED_MOMENTUM_BPS", str(DEFAULT_MIN_MOMENTUM_BPS))),
        )

    def update(self, tick):
        snap = super().update(tick)
        if snap.direction == 0:
            return snap
        aligned_momentum_bps = snap.direction * snap.momentum_bps
        if aligned_momentum_bps + 1e-12 >= self._min_momentum_bps:
            return snap
        return hybrid.MicrostructureSnapshot(
            direction=0,
            confidence=snap.confidence,
            trade_rate=snap.trade_rate,
            buy_ratio=snap.buy_ratio,
            cvd_norm=snap.cvd_norm,
            momentum_bps=snap.momentum_bps,
            price_changes=snap.price_changes,
        )


class _SpreadAwareExitPolicy(hybrid.AsymmetricExitPolicy):
    """Keep the winner trail at least as wide as the most recent Demo spread.

    The underlying staged policy only ratchets its positive stop upward. Once
    executable MFE arms a positive lock, a later spread spike cannot loosen it.
    """

    def on_tick(self, **kwargs):
        if _LAST_DEMO_SPREAD_BPS is not None and _LAST_DEMO_SPREAD_BPS > 0:
            self.trailing_distance_bps = max(
                self.winner_pullback_bps,
                _LAST_DEMO_SPREAD_BPS,
            )
        return super().on_tick(**kwargs)


class _LiveSignalMarket:
    """Force Hybrid's signal/tape source to LIVE MEXC while leaving execution Demo-only."""

    def __new__(cls, *_args, **_kwargs):
        return MexcPublicMarket(LIVE_REST, LIVE_WS)


class _GuardedDemoAdapter(MexcWebExecutionAdapter):
    """Demo execution adapter with strict LIVE L2 and TESTNET consistency guards."""

    def __init__(self, config):
        super().__init__(config)
        self._live_market = MexcPublicMarket(LIVE_REST, LIVE_WS)
        self._max_divergence_bps = float(
            os.getenv("MEXC_DEMO_LIVE_MAX_DIVERGENCE_BPS", str(DEFAULT_MAX_DIVERGENCE_BPS))
        )
        self._book_veto_confidence = float(
            os.getenv("MEXC_BOOK_VETO_CONFIDENCE", str(DEFAULT_BOOK_VETO_CONFIDENCE))
        )
        self._book_confirm_confidence = float(
            os.getenv("MEXC_BOOK_CONFIRM_CONFIDENCE", str(DEFAULT_BOOK_CONFIRM_CONFIDENCE))
        )
        self._require_book_confirmation = _env_yes(
            "MEXC_REQUIRE_BOOK_CONFIRMATION", DEFAULT_REQUIRE_BOOK_CONFIRMATION
        )
        self.last_divergence_bps: float | None = None
        self.last_demo_spread_bps: float | None = None
        self._demo_best_cache: dict[OrderSide, tuple[float, float]] = {}
        self._live_mid_cache: tuple[float, float] | None = None
        self._auto_flatten_start = os.getenv(AUTO_FLATTEN_ENV, "NO").upper() == "YES"
        self._entry_started = False
        self._cleaning_start_residual = False

    async def get_position(self, symbol: str):
        position = await super().get_position(symbol)
        if (
            position is not None
            and self._auto_flatten_start
            and not self._entry_started
            and not self._cleaning_start_residual
        ):
            self._cleaning_start_residual = True
            try:
                hybrid.console.print(
                    f"[yellow]LATE START RESIDUAL[/yellow]: Demo {symbol} "
                    f"{'LONG' if position.side is OrderSide.LONG else 'SHORT'} qty={position.qty:g}; flattening before entry"
                )
                await hybrid._flatten_position(self, position, "late_startup_auto_flatten")
                residual = await super().get_position(symbol)
                if residual is not None and residual.qty > 1e-12:
                    raise MexcWebError(
                        f"late startup auto-flatten failed; residual qty={residual.qty}"
                    )
                hybrid.console.print(
                    f"[green]LATE START RESIDUAL CLEARED[/green]: {symbol} is flat; waiting for a fresh signal"
                )
                return None
            finally:
                self._cleaning_start_residual = False
        return position

    def _skipped_fill(
        self,
        *,
        symbol: str,
        side: OrderSide,
        qty: float,
        price: float,
        client_order_id: str,
    ) -> OrderFill:
        return OrderFill(
            symbol=symbol,
            side=side,
            requested_qty=qty,
            filled_qty=0.0,
            avg_price=price,
            fee_usdt=0.0,
            order_id="",
            client_order_id=client_order_id,
            position_id=None,
        )

    async def _wait_position_visible(self, symbol: str, side: OrderSide):
        deadline = time.monotonic() + IOC_POSITION_VISIBLE_SECONDS
        while time.monotonic() < deadline:
            remote = await super().get_position(symbol)
            if remote is not None:
                if remote.side is not side:
                    raise MexcWebError(
                        f"IOC visible-position side mismatch for {symbol}: requested={side.value} remote={remote.side.value}"
                    )
                return remote
            await asyncio.sleep(IOC_POSITION_VISIBLE_POLL_SECONDS)
        return None

    async def open_ioc(self, *args, **kwargs):
        symbol = str(kwargs.get("symbol") or "").upper()
        side = kwargs.get("side")
        qty = float(kwargs.get("qty") or 0.0)
        price = float(kwargs.get("price") or 0.0)
        client_order_id = str(kwargs.get("client_order_id") or "")

        if not symbol or side not in (OrderSide.LONG, OrderSide.SHORT):
            raise MexcWebError("L2 entry gate requires symbol and LONG/SHORT side")

        try:
            depth = await self._live_market.depth(symbol, limit=10)
            book = analyze_order_book(depth.bids, depth.asks, depth=5) if depth is not None else None
            trade_direction = 1 if side is OrderSide.LONG else -1
            decision = book_confirmation(
                trade_direction=trade_direction,
                book=book,
                veto_confidence=self._book_veto_confidence,
                confirm_confidence=self._book_confirm_confidence,
            )
            if book is not None and book.valid:
                hybrid.console.print(
                    f"BOOK GATE side={'LONG' if side is OrderSide.LONG else 'SHORT'} "
                    f"decision={decision.reason} OBI={book.imbalance:+.3f} "
                    f"micro={book.microprice_edge_bps:+.3f}bps pressure={book.pressure:+.3f} "
                    f"book_conf={book.confidence:.3f} spread={book.spread_bps:.3f}bps"
                )
            if not _strict_book_allows(decision, self._require_book_confirmation):
                hybrid.console.print(
                    f"[yellow]IOC SKIPPED BY L2[/yellow]: {symbol} "
                    f"{'LONG' if side is OrderSide.LONG else 'SHORT'} reason={decision.reason}"
                )
                return self._skipped_fill(
                    symbol=symbol,
                    side=side,
                    qty=qty,
                    price=price,
                    client_order_id=client_order_id,
                )
        except (aiohttp.ClientError, TimeoutError) as exc:
            hybrid.console.print(
                f"[yellow]IOC SKIPPED: L2 UNAVAILABLE[/yellow] {symbol}: {type(exc).__name__}"
            )
            return self._skipped_fill(
                symbol=symbol,
                side=side,
                qty=qty,
                price=price,
                client_order_id=client_order_id,
            )
        except MexcWebError:
            raise
        except Exception as exc:
            hybrid.console.print(
                f"[yellow]IOC SKIPPED: L2 ERROR[/yellow] {symbol}: {type(exc).__name__}: {exc}"
            )
            return self._skipped_fill(
                symbol=symbol,
                side=side,
                qty=qty,
                price=price,
                client_order_id=client_order_id,
            )

        self._entry_started = True
        fill = await super().open_ioc(*args, **kwargs)
        if fill.filled_qty > 0:
            visible = await self._wait_position_visible(symbol, side)
            if visible is None:
                hybrid.console.print(
                    f"[yellow]IOC POSITION PENDING[/yellow] {symbol} "
                    f"reported_fill={fill.filled_qty:g}; deferring to late-position recovery"
                )
                return _as_pending_fill(fill)
        return fill

    async def close_market_reduce_only(self, *args, **kwargs):
        """Retry only transient TESTNET position-visibility misses before closing."""
        last_error: MexcWebError | None = None
        for attempt in range(CLOSE_POSITION_RETRIES):
            try:
                return await super().close_market_reduce_only(*args, **kwargs)
            except MexcWebError as exc:
                if "no open position for" not in str(exc):
                    raise
                last_error = exc
                if attempt + 1 < CLOSE_POSITION_RETRIES:
                    await asyncio.sleep(CLOSE_POSITION_RETRY_SECONDS)
        assert last_error is not None
        raise last_error

    async def _get_live_mid(self, symbol: str) -> float:
        now = time.monotonic()
        if self._live_mid_cache is not None:
            cached_mid, cached_at = self._live_mid_cache
            if now - cached_at <= LIVE_TICKER_CACHE_SECONDS:
                return cached_mid

        live = await self._live_market.ticker(symbol)
        if live is None:
            raise MexcWebError(f"LIVE ticker unavailable for {symbol}")
        live_bid = float(live.bid or 0)
        live_ask = float(live.ask or 0)
        if live_bid <= 0 or live_ask <= 0:
            raise MexcWebError(f"LIVE bid/ask unavailable for {symbol}")

        live_mid = (live_bid + live_ask) / 2.0
        self._live_mid_cache = (live_mid, now)
        return live_mid

    async def get_best_price(self, symbol: str, side: OrderSide) -> float:
        global _LAST_DEMO_SPREAD_BPS

        demo_price = await super().get_best_price(symbol, side)
        now = time.monotonic()
        self._demo_best_cache[side] = (demo_price, now)
        ask_row = self._demo_best_cache.get(OrderSide.LONG)
        bid_row = self._demo_best_cache.get(OrderSide.SHORT)
        if ask_row is not None and bid_row is not None:
            ask, ask_ts = ask_row
            bid, bid_ts = bid_row
            if abs(ask_ts - bid_ts) <= DEMO_SPREAD_CACHE_SECONDS and ask > bid > 0:
                mid = (ask + bid) / 2.0
                spread_bps = (ask - bid) / mid * 10_000.0
                self.last_demo_spread_bps = spread_bps
                _LAST_DEMO_SPREAD_BPS = spread_bps

        live_mid = await self._get_live_mid(symbol)
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
            for _ in range(4):
                await asyncio.sleep(0.35)
                existing = await adapter.get_position(symbol)
                if existing is not None:
                    break
            if existing is None:
                hybrid.console.print(f"[green]START FLAT CONFIRMED[/green]: {symbol} remained flat")
                return
        if existing is not None:
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
        os.environ[AUTO_FLATTEN_ENV] = "YES"

    symbol = (_arg_value("--symbol") or "").upper()
    if auto_flatten:
        if not symbol:
            raise SystemExit("--auto-flatten-start requires --symbol")
        try:
            asyncio.run(_auto_flatten_demo_start(symbol))
        except MexcWebError as exc:
            hybrid.console.print(f"[red]LIVE-SIGNAL DEMO FAILED:[/red] {exc}")
            raise SystemExit(2) from exc

    hybrid.MexcPublicMarket = _LiveSignalMarket
    hybrid.MexcWebExecutionAdapter = _GuardedDemoAdapter
    hybrid.MicrostructureSignal = _MomentumAlignedSignal
    hybrid.AsymmetricExitPolicy = _SpreadAwareExitPolicy
    hybrid.POSITION_WATCHDOG_SECONDS = FAST_POSITION_WATCHDOG_SECONDS
    hybrid.console.print(
        "[cyan]LIVE SIGNAL / DEMO EXECUTION MODE[/cyan]: signal ticks=LIVE MEXC, "
        "strict aligned L2 confirmation=LIVE MEXC, orders/positions=TESTNET only"
    )
    hybrid.console.print(
        f"[cyan]PROFIT GUARDS[/cyan]: fee must remain 0/0; momentum aligned >= "
        f"{DEFAULT_MIN_MOMENTUM_BPS:.2f}bps; book confirmation required; "
        f"watchdog={FAST_POSITION_WATCHDOG_SECONDS:.2f}s; positive staged trailing enabled"
    )
    hybrid.main()


if __name__ == "__main__":
    main()
