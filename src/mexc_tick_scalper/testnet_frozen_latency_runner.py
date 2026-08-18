from __future__ import annotations

import asyncio
import time
from pathlib import Path

from rich.console import Console

from .baseline_v1 import BASELINE_V1, apply_baseline_v1
from .execution import OrderSide
from .live_zero_fee_universe import discover_live_zero_fee_crosslisted
from .microspread_feed import EventMexcDepthFeed
from .persistent_lag_profile import build_profiles, latest_lifetime_csv, select_profiles
from .testnet_known_good_v1 import (
    _same_symbol_universe as _execution_only_testnet_universe,
    _testnet_contract_rows,
)
from . import testnet_known_good_risk as risk
from .web_execution import MexcWebError, MexcWebExecutionAdapter

console = Console()
REFERENCE_COMMIT = "8a0bc6043385dbaf95ec8e77b93d91fd00a7f9e5"
TESTNET_WS = "wss://futures.testnet.mexc.com/edge"
TESTNET_QUOTE_MAX_AGE_MS = 1200
_LIFETIME_CSV = ""
_FIDELITY_MODE = "UNSET"
_TESTNET_FEED: EventMexcDepthFeed | None = None
_TESTNET_WAKE: asyncio.Event | None = None
_ORIGINAL_GET_BEST_PRICE = MexcWebExecutionAdapter.get_best_price
_ORIGINAL_OPEN_IOC = MexcWebExecutionAdapter.open_ioc
_ORIGINAL_RISK_POSITION = risk.RiskPosition
_LAST_IOC_SUBMIT_MONO = 0.0
_LAST_IOC_SUBMIT_WALL_MS = 0


async def _start_testnet_ws_cache(symbols: list[str]) -> None:
    """Keep Testnet executable quotes local so entry does not spend a REST RTT before POST."""
    global _TESTNET_FEED, _TESTNET_WAKE
    if _TESTNET_FEED is not None:
        return
    _TESTNET_WAKE = asyncio.Event()
    _TESTNET_FEED = EventMexcDepthFeed(
        symbols,
        None,
        _TESTNET_WAKE,
        depth_limit=5,
        ws_url=TESTNET_WS,
    )
    await _TESTNET_FEED.start()

    # Give the feed a bounded startup window. Missing individual symbols still fall back to REST.
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        if _TESTNET_FEED.books:
            break
        try:
            await asyncio.wait_for(_TESTNET_WAKE.wait(), timeout=0.25)
        except TimeoutError:
            pass
        _TESTNET_WAKE.clear()

    console.print(
        f"TESTNET WS QUOTE CACHE ready={len(_TESTNET_FEED.books)}/{len(symbols)} "
        f"(REST fallback only for missing/stale quotes)"
    )


async def _cached_get_best_price(self: MexcWebExecutionAdapter, symbol: str, side: OrderSide) -> float:
    feed = _TESTNET_FEED
    if feed is not None:
        book = feed.books.get(symbol.upper())
        now_ms = int(time.time() * 1000)
        if book is not None and now_ms - book.recv_ms <= TESTNET_QUOTE_MAX_AGE_MS:
            return book.ask if side is OrderSide.LONG else book.bid
    return await _ORIGINAL_GET_BEST_PRICE(self, symbol, side)


async def _timed_open_ioc(self: MexcWebExecutionAdapter, **kwargs):
    """Capture the position clock at IOC POST path start, not after slow reconciliation."""
    global _LAST_IOC_SUBMIT_MONO, _LAST_IOC_SUBMIT_WALL_MS
    _LAST_IOC_SUBMIT_MONO = time.monotonic()
    _LAST_IOC_SUBMIT_WALL_MS = int(time.time() * 1000)
    return await _ORIGINAL_OPEN_IOC(self, **kwargs)


def _risk_position_from_submit(*args, **kwargs):
    """Correct Testnet holding-time origin to the actual IOC submit attempt.

    The old runner constructed RiskPosition only after order confirmation + position visibility,
    hiding hundreds of milliseconds in which the exchange position could already exist.
    """
    if _LAST_IOC_SUBMIT_MONO > 0:
        if len(args) >= 4:
            rows = list(args)
            rows[2] = _LAST_IOC_SUBMIT_WALL_MS
            rows[3] = _LAST_IOC_SUBMIT_MONO
            args = tuple(rows)
        else:
            kwargs["entry_ts_ms"] = _LAST_IOC_SUBMIT_WALL_MS
            kwargs["entry_mono"] = _LAST_IOC_SUBMIT_MONO
    return _ORIGINAL_RISK_POSITION(*args, **kwargs)


async def _frozen_execution_universe(
    adapter: MexcWebExecutionAdapter,
) -> tuple[list, dict[str, dict]]:
    """Use frozen production eligibility when possible; otherwise Testnet is execution-only.

    FULL_FIDELITY means the symbol passed the original persistent-profile + LIVE exact-0/0
    + Binance cross-listing gates and is also executable on Testnet.

    TESTNET_EXECUTION_ONLY means Testnet exposes none of the currently frozen-production
    symbols. We then use only same-symbol Binance + LIVE MEXC + Testnet instruments to
    validate real IOC/partial-fill/reduce-only/risk mechanics. BASELINE_V1 signal/entry/exit
    thresholds remain frozen, but results from that fallback MUST NOT be presented as a
    faithful strategy-PnL validation because pair eligibility differs.
    """
    global _FIDELITY_MODE

    source = Path(_LIFETIME_CSV) if _LIFETIME_CSV else latest_lifetime_csv(Path.cwd())
    profiles = select_profiles(
        build_profiles(source),
        min_signals=BASELINE_V1["pair_min_signals"],
        min_median_lifetime_ms=BASELINE_V1["pair_min_median_lifetime_ms"],
        min_survival_rate=BASELINE_V1["pair_min_survival_rate"],
        min_signal_strength_ratio=BASELINE_V1["pair_min_strength_ratio"],
    )
    persistent = {p.symbol for p in profiles}

    production = [c for c in await discover_live_zero_fee_crosslisted() if c.mexc_symbol in persistent]
    if not production:
        raise MexcWebError("Frozen baseline has no currently eligible persistent exact-0/0 LIVE pair")

    testnet_rows = await _testnet_contract_rows(adapter)
    selected = []
    details: dict[str, dict] = {}
    unavailable: list[str] = []
    unusable: list[str] = []

    for contract in production:
        symbol = contract.mexc_symbol
        row = testnet_rows.get(symbol)
        if row is None:
            unavailable.append(symbol)
            continue
        try:
            ask, bid = await asyncio.gather(
                _ORIGINAL_GET_BEST_PRICE(adapter, symbol, OrderSide.LONG),
                _ORIGINAL_GET_BEST_PRICE(adapter, symbol, OrderSide.SHORT),
            )
        except MexcWebError:
            unusable.append(symbol)
            continue
        if ask <= 0 or bid <= 0 or ask < bid:
            unusable.append(symbol)
            continue
        selected.append(contract)
        details[symbol] = row

    console.print(
        f"[bold cyan]FROZEN 8a0bc60 PRODUCTION ELIGIBILITY[/bold cyan] "
        f"persistent+LIVE exact0/0+Binance={len(production)}; Testnet executable={len(selected)}"
    )
    if unavailable:
        console.print("TESTNET UNAVAILABLE: " + ",".join(sorted(unavailable)))
    if unusable:
        console.print("TESTNET BOOK UNUSABLE: " + ",".join(sorted(unusable)))

    if selected:
        _FIDELITY_MODE = "FULL_FIDELITY"
        await _start_testnet_ws_cache([c.mexc_symbol for c in selected])
        console.print("[bold green]MODE=FULL_FIDELITY[/bold green] strategy universe and Testnet execution overlap.")
        return selected, details

    fallback, fallback_details = await _execution_only_testnet_universe(adapter)
    _FIDELITY_MODE = "TESTNET_EXECUTION_ONLY"
    await _start_testnet_ws_cache([c.mexc_symbol for c in fallback])
    console.print(
        "[bold yellow]MODE=TESTNET_EXECUTION_ONLY[/bold yellow] No frozen-production symbol exists on Testnet right now."
    )
    console.print(
        "Frozen BASELINE_V1 thresholds/exits remain unchanged, but pair eligibility is NOT equivalent. "
        "Use these trades only to validate real IOC partial fills, position reconciliation, fees, liquidation safeguards "
        "and reduce-only exits; do not compare this PnL with the 8a0bc60 strategy result."
    )
    console.print("EXECUTION-ONLY TESTNET UNIVERSE: " + ",".join(c.mexc_symbol for c in fallback))
    return fallback, fallback_details


def _mode_summary(original_summary):
    def wrapped(stats, target):
        return f"MODE={_FIDELITY_MODE} " + original_summary(stats, target)
    return wrapped


async def _run_with_cleanup(args) -> None:
    try:
        await risk.run(args)
    finally:
        if _TESTNET_FEED is not None:
            await _TESTNET_FEED.close()


def main() -> None:
    global _LIFETIME_CSV
    args = risk.build_parser().parse_args()
    apply_baseline_v1(args)
    _LIFETIME_CSV = args.lifetime_csv

    if args.target_closed_trades <= 0 or args.risk_max_leverage <= 0:
        raise SystemExit("target_closed_trades and risk_max_leverage must be positive")
    if args.emergency_liq_distance_bps >= args.min_liq_distance_bps:
        raise SystemExit("emergency_liq_distance_bps must be lower than min_liq_distance_bps")

    risk._same_symbol_universe = _frozen_execution_universe
    risk.KNOWN_GOOD_COMMIT = REFERENCE_COMMIT
    risk._summary = _mode_summary(risk._summary)
    risk.RiskPosition = _risk_position_from_submit
    MexcWebExecutionAdapter.get_best_price = _cached_get_best_price
    MexcWebExecutionAdapter.open_ioc = _timed_open_ioc

    console.print(f"[bold]FROZEN LATENCY REFERENCE[/bold] {REFERENCE_COMMIT}")
    console.print(
        "BASELINE_V1 is forcibly applied. The original persistent profile + LIVE exact 0/0 + Binance universe is "
        "always evaluated first. Testnet executable quotes are cached by WebSocket so entry does not pay an extra "
        "REST depth RTT before IOC POST. Position hold-time starts at IOC submit, not delayed position visibility."
    )
    try:
        asyncio.run(_run_with_cleanup(args))
    except MexcWebError as exc:
        console.print(f"[red]FROZEN TESTNET RUN FAILED:[/red] {exc}")
        raise SystemExit(2) from exc


if __name__ == "__main__":
    main()
