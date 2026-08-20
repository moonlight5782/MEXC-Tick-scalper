from __future__ import annotations

import asyncio
import math
import statistics
import time
from dataclasses import dataclass, field

from rich.table import Table

from . import auto_discovery_shadow as auto
from . import auto_discovery_testnet_xrp_fixed as fixed
from . import auto_discovery_testnet_xrp_profit_hold as profit_hold
from . import auto_discovery_testnet_xrp_runtime_diag as runtime_diag
from .lead_lag import fetch_binance_usdm_symbols, mexc_to_binance_symbol
from .lead_lag_strategy import LeadLagGate
from .live_zero_fee_universe import LIVE_REST, LIVE_WS, LiveZeroFeeContract
from .market import MexcPublicMarket
from .microspread import MicroSpreadModel
from .microspread_feed import EventBinanceBookTickerFeed, EventMexcDepthFeed
from .persistent_lag_profile import PairLagProfile
from .prelive_persistent_ioc_shadow_v2 import _event_key, _valid_snapshot
from .web_execution import MexcWebExecutionAdapter, WebExecutionConfig
from .web_fee import read_web_fee_provider


@dataclass(frozen=True, slots=True)
class LiveScanContract:
    contract: LiveZeroFeeContract

    @property
    def symbol(self) -> str:
        return self.contract.mexc_symbol


@dataclass(frozen=True, slots=True)
class TestnetContract:
    contract: LiveZeroFeeContract
    maker_fee: float | None
    taker_fee: float | None
    demo_max_leverage: int
    demo_rtt_ms: float

    @property
    def symbol(self) -> str:
        return self.contract.mexc_symbol


@dataclass(slots=True)
class ScanSignal:
    started_ms: int
    direction: int
    residual_bps: float
    strength: float
    lifetime_ms: float | None = None
    terminal_reason: str = ""


@dataclass(slots=True)
class ScanStats:
    signals: list[ScanSignal] = field(default_factory=list)
    active: ScanSignal | None = None


@dataclass(frozen=True, slots=True)
class SelectableCandidate:
    candidate: auto.Candidate
    maker_fee: float | None
    taker_fee: float | None
    demo_max_leverage: int
    demo_rtt_ms: float

    @property
    def symbol(self) -> str:
        return self.candidate.profile.symbol


def _median(values: list[float]) -> float:
    return statistics.median(values) if values else 0.0


def _fee_bps(value: float | None) -> str:
    return "?" if value is None else f"{value * 10_000.0:.2f}bps"


def _zero_fee_choice(raw: str) -> bool:
    choice = raw.strip().lower()
    if choice in {"", "y", "yes", "д", "да"}:
        return True
    if choice in {"n", "no", "н", "нет"}:
        return False
    raise ValueError("enter Y/Yes or N/No")


def _terminal_reason(signal: ScanSignal, residual_bps: float, args) -> str | None:
    direction = 1 if residual_bps > 0 else -1 if residual_bps < 0 else 0
    if direction == -signal.direction and abs(residual_bps) >= args.reversal_edge_bps:
        return "residual_reversal"
    convergence = max(args.convergence_bps, abs(signal.residual_bps) * args.convergence_fraction)
    if abs(residual_bps) <= convergence:
        return "convergence"
    return None


def _contract_rows(payload) -> list[dict]:
    data = payload.get("data") if isinstance(payload, dict) else payload
    if isinstance(data, list):
        return [row for row in data if isinstance(row, dict)]
    if isinstance(data, dict):
        if "symbol" in data:
            return [data]
        return [row for row in data.values() if isinstance(row, dict)]
    return []


async def _discover_live_crosslisted() -> list[LiveScanContract]:
    """Build Binance USD-M x LIVE MEXC Futures universe without private LIVE auth."""
    fixed.console.print(
        "[cyan][1/4][/cyan] Loading public Binance USD-M + LIVE MEXC contracts (no LIVE web token required)..."
    )
    binance_symbols = await fetch_binance_usdm_symbols()
    market = MexcPublicMarket(LIVE_REST, LIVE_WS)
    mexc_rows = await market.contracts()

    out: list[LiveScanContract] = []
    for row in mexc_rows:
        symbol = str(row.get("symbol") or "").upper()
        if not symbol:
            continue
        binance_symbol = mexc_to_binance_symbol(symbol)
        if binance_symbol not in binance_symbols:
            continue
        contract_size = float(row.get("contractSize") or 0)
        if contract_size <= 0:
            continue

        contract = LiveZeroFeeContract(
            mexc_symbol=symbol,
            binance_symbol=binance_symbol,
            max_leverage=int(row.get("maxLeverage") or 1),
            contract_size=contract_size,
            min_vol=float(row.get("minVol") or 0),
            maintenance_margin_rate=float(row.get("maintenanceMarginRate") or 0),
            initial_margin_rate=float(row.get("initialMarginRate") or 0),
            risk_base_vol=float(row.get("riskBaseVol") or 0),
            risk_incr_vol=float(row.get("riskIncrVol") or 0),
            risk_incr_mmr=float(row.get("riskIncrMmr") or 0),
            risk_level_limit=max(1, int(row.get("riskLevelLimit") or 1)),
            risk_limit_type=str(row.get("riskLimitType") or "BY_VOLUME").upper(),
        )
        out.append(LiveScanContract(contract))

    out.sort(key=lambda item: item.symbol)
    fixed.console.print(f"[cyan][1/4][/cyan] Public Binance/MEXC cross-listed contracts={len(out)}")
    return out


async def _testnet_universe(
    contracts: list[LiveScanContract], *, zero_fee_only: bool
) -> list[TestnetContract]:
    """Intersect with Testnet and read Testnet fee rates using DEMO auth only."""
    fixed.console.print(
        f"[cyan][2/4][/cyan] Loading MEXC Testnet contracts + Demo fee rates for {len(contracts)} candidates..."
    )
    cfg = WebExecutionConfig.demo_from_env(write_enabled=False)
    async with MexcWebExecutionAdapter(cfg) as adapter:
        started = time.perf_counter_ns()
        payload = await adapter._request("GET", "/contract/detail")
        elapsed_ms = (time.perf_counter_ns() - started) / 1_000_000.0
        fee_provider = await read_web_fee_provider(adapter)

    detail_by_symbol: dict[str, dict] = {}
    for detail in _contract_rows(payload):
        symbol = str(detail.get("symbol") or "").upper()
        if symbol:
            detail_by_symbol[symbol] = detail

    rows: list[TestnetContract] = []
    confirmed_zero = 0
    fee_unknown = 0
    for live_row in contracts:
        detail = detail_by_symbol.get(live_row.symbol)
        if detail is None:
            continue
        contract_size = float(detail.get("contractSize") or 0)
        price_unit = float(detail.get("priceUnit") or 0)
        max_lev = int(detail.get("maxLeverage") or 1)
        if contract_size <= 0 or price_unit <= 0 or max_lev <= 0:
            continue

        fee_status = fee_provider.status(live_row.symbol)
        maker = fee_status.maker
        taker = fee_status.taker
        is_zero = maker == 0.0 and taker == 0.0
        confirmed_zero += int(is_zero)
        fee_unknown += int(maker is None or taker is None)
        if zero_fee_only and not is_zero:
            continue

        rows.append(
            TestnetContract(
                contract=live_row.contract,
                maker_fee=maker,
                taker_fee=taker,
                demo_max_leverage=max_lev,
                demo_rtt_ms=elapsed_ms,
            )
        )

    scope = "0/0 only" if zero_fee_only else "all fees"
    fixed.console.print(
        f"[cyan][2/4][/cyan] Testnet contracts={len(detail_by_symbol)}; "
        f"confirmed Demo 0/0={confirmed_zero}; fee_unknown={fee_unknown}; "
        f"usable intersection={len(rows)} ({scope}); metadata RTT={elapsed_ms:.0f}ms"
    )
    return rows


def _score_profile(profile: PairLagProfile, survival: float) -> float:
    evidence = math.log1p(max(0, profile.signals))
    convergence_quality = max(0.10, profile.convergence_rate + 0.25 * (1.0 - profile.reversal_rate))
    return (
        max(0.0, survival)
        * max(0.0, profile.median_signal_residual_bps)
        * max(0.0, profile.median_signal_strength_ratio)
        * evidence
        * convergence_quality
    )


def _candidate_from_scan(row: TestnetContract, stats: ScanStats, scan_end_ms: int) -> SelectableCandidate | None:
    if not stats.signals:
        return None

    lifetimes: list[float] = []
    residuals: list[float] = []
    strengths: list[float] = []
    convergences = 0
    reversals = 0
    survived = 0

    for signal in stats.signals:
        lifetime = signal.lifetime_ms
        if lifetime is None:
            lifetime = max(0.0, float(scan_end_ms - signal.started_ms))
        lifetimes.append(lifetime)
        residuals.append(abs(signal.residual_bps))
        strengths.append(signal.strength)
        if lifetime >= row.demo_rtt_ms:
            survived += 1
        if signal.terminal_reason == "convergence":
            convergences += 1
        elif signal.terminal_reason == "residual_reversal":
            reversals += 1

    n = len(lifetimes)
    survival = survived / n if n else 0.0
    profile = PairLagProfile(
        symbol=row.symbol,
        signals=n,
        median_lifetime_ms=_median(lifetimes),
        p75_lifetime_ms=0.0,
        p90_lifetime_ms=0.0,
        survive_execution_rate=survival,
        convergence_rate=convergences / n if n else 0.0,
        reversal_rate=reversals / n if n else 0.0,
        median_signal_residual_bps=_median(residuals),
        median_signal_strength_ratio=_median(strengths),
        median_leader_advantage_bps=0.0,
    )
    candidate = auto.Candidate(
        profile=profile,
        contract=row.contract,
        current_survival=survival,
        score=_score_profile(profile, survival),
    )
    return SelectableCandidate(
        candidate=candidate,
        maker_fee=row.maker_fee,
        taker_fee=row.taker_fee,
        demo_max_leverage=row.demo_max_leverage,
        demo_rtt_ms=row.demo_rtt_ms,
    )


async def _scan_live_candidates(args, *, zero_fee_only: bool) -> list[SelectableCandidate]:
    """Fresh PRE-TRADE scan. No historical lifetime CSV is read here."""
    contracts = await _discover_live_crosslisted()
    if not contracts:
        raise RuntimeError("No current Binance/MEXC cross-listed Futures contracts were found")

    universe = await _testnet_universe(contracts, zero_fee_only=zero_fee_only)
    if not universe:
        scope = " with explicit Demo maker=0/taker=0" if zero_fee_only else ""
        raise RuntimeError(f"No Binance/MEXC contract{scope} is also usable on MEXC Testnet")

    feed_contracts = [row.contract for row in universe]
    symbols = [row.symbol for row in universe]
    models = {
        row.symbol: MicroSpreadModel(
            horizon_ms=args.micro_horizon_ms,
            baseline_seconds=args.baseline_seconds,
            baseline_exclusion_ms=args.baseline_exclusion_ms,
            min_edge_bps=0.0,
            min_binance_move_bps=0.0,
            max_binance_age_ms=args.max_binance_age_ms,
            max_mexc_age_ms=args.max_mexc_age_ms,
        )
        for row in universe
    }
    gate = LeadLagGate(
        noise_window_ms=args.noise_window_ms,
        residual_noise_multiplier=args.residual_noise_multiplier,
        binance_noise_multiplier=args.binance_noise_multiplier,
        min_edge_bps=args.min_edge_bps,
        min_net_edge_bps=args.min_net_edge_bps,
        spread_ratio=args.edge_to_spread_ratio,
        min_binance_move_bps=args.min_binance_move_bps,
        min_leader_advantage_bps=args.min_leader_advantage_bps,
        min_lead_ratio=args.min_lead_ratio,
        confirm_updates=args.confirm_updates,
        confirm_ms=args.confirm_ms,
        rearm_fraction=args.rearm_fraction,
    )

    wake = asyncio.Event()
    binance = EventBinanceBookTickerFeed(feed_contracts, models, wake)
    mexc = EventMexcDepthFeed(symbols, models, wake, depth_limit=args.depth_limit)
    stats = {symbol: ScanStats() for symbol in symbols}

    fixed.console.print(f"[cyan][3/4][/cyan] Starting LIVE signal feeds for {len(symbols)} pairs...")
    await binance.start()
    await mexc.start()
    fee_scope = "Demo 0/0 only" if zero_fee_only else "all Demo fees (reporting-only in Testnet)"
    fixed.console.print(
        f"[cyan][3/4][/cyan] LIVE scan active: fees={fee_scope}; warmup={args.warmup_seconds:g}s; "
        f"sample={args.scan_seconds:g}s; qualifying signal=baseline >=8bps AND >=3x."
    )

    warmup_until = time.monotonic() + args.warmup_seconds
    deadline = warmup_until + args.scan_seconds
    started_scan = time.monotonic()
    next_progress = started_scan + 5.0
    try:
        while time.monotonic() < deadline:
            now = time.monotonic()
            now_ms = int(time.time() * 1000)
            if now >= warmup_until:
                for symbol in symbols:
                    book = mexc.books.get(symbol)
                    if book is None or now_ms - book.recv_ms > args.max_book_age_ms:
                        continue
                    model = models[symbol]
                    snap = model.snapshot(now_ms=now_ms, threshold_bps=0.0)
                    if not _valid_snapshot(snap):
                        continue

                    bucket = stats[symbol]
                    if bucket.active is not None:
                        reason = _terminal_reason(bucket.active, float(snap.edge_bps), args)
                        if reason is not None:
                            bucket.active.lifetime_ms = max(0.0, float(now_ms - bucket.active.started_ms))
                            bucket.active.terminal_reason = reason
                            bucket.active = None

                    decision = gate.observe(
                        symbol,
                        snap,
                        book.spread_bps,
                        now_ms,
                        event_key=_event_key(model),
                    )
                    strength = abs(decision.residual_bps) / max(decision.threshold_bps, 1e-12)
                    if (
                        bucket.active is None
                        and decision.ready
                        and strength >= args.min_signal_strength_ratio
                        and abs(decision.residual_bps) >= args.min_absolute_residual_bps
                    ):
                        signal = ScanSignal(
                            started_ms=now_ms,
                            direction=decision.direction,
                            residual_bps=float(decision.residual_bps),
                            strength=float(strength),
                        )
                        bucket.signals.append(signal)
                        bucket.active = signal

            if now >= next_progress:
                signal_count = sum(len(bucket.signals) for bucket in stats.values())
                signaled_pairs = sum(bool(bucket.signals) for bucket in stats.values())
                phase = "warmup" if now < warmup_until else "sampling"
                fixed.console.print(
                    f"[cyan]SCAN[/cyan] {phase} elapsed={now-started_scan:.0f}s "
                    f"qualifying_signals={signal_count} pairs_with_signal={signaled_pairs}"
                )
                next_progress = now + 5.0

            wake.clear()
            try:
                await asyncio.wait_for(wake.wait(), timeout=0.25)
            except TimeoutError:
                pass
    finally:
        await binance.close()
        await mexc.close()

    scan_end_ms = int(time.time() * 1000)
    candidates = [
        candidate
        for row in universe
        if (candidate := _candidate_from_scan(row, stats[row.symbol], scan_end_ms)) is not None
    ]
    candidates.sort(key=lambda row: row.candidate.score, reverse=True)
    if args.discovery_top > 0:
        candidates = candidates[: args.discovery_top]
    fixed.console.print(
        f"[cyan][4/4][/cyan] Scan complete: qualifying candidates={len(candidates)}. Scanner feeds are stopped."
    )
    return candidates


def _show_selectable(rows: list[SelectableCandidate]) -> None:
    table = Table(title="Fresh Binance + MEXC lead-lag scan; executable on MEXC Testnet")
    for col in (
        "#", "Symbol", "Signals", "Med lag", "Survive@DemoRTT", "Residual",
        "Strength", "Demo maker", "Demo taker", "LIVE lev", "Demo lev", "Demo RTT", "Score",
    ):
        table.add_column(col)
    for idx, row in enumerate(rows, 1):
        candidate = row.candidate
        p = candidate.profile
        table.add_row(
            str(idx),
            p.symbol,
            str(p.signals),
            f"{p.median_lifetime_ms:.0f}ms",
            f"{candidate.current_survival:.0%}",
            f"{p.median_signal_residual_bps:.1f}bps",
            f"{p.median_signal_strength_ratio:.2f}x",
            _fee_bps(row.maker_fee),
            _fee_bps(row.taker_fee),
            f"{candidate.contract.max_leverage}x",
            f"{row.demo_max_leverage}x",
            f"{row.demo_rtt_ms:.0f}ms",
            f"{candidate.score:.1f}",
        )
    fixed.console.print(table)


def _select(rows: list[SelectableCandidate], raw: str) -> SelectableCandidate:
    if not rows:
        raise ValueError("no selectable candidates")
    choice = raw.strip().upper()
    if not choice:
        return rows[0]
    if choice.isdigit():
        idx = int(choice)
        if idx < 1 or idx > len(rows):
            raise ValueError(f"pair number must be between 1 and {len(rows)}")
        return rows[idx - 1]
    normalized = choice if choice.endswith("_USDT") else f"{choice}_USDT"
    for row in rows:
        if row.symbol == normalized:
            return row
    raise ValueError(f"{choice!r} is not in the current candidate list")


async def _run_selected(args) -> None:
    fixed.console.print("[bold cyan]PRE-TRADE PAIR SCAN[/bold cyan]")
    fixed.console.print(
        "Fresh observation only: no historical lifetime CSV. Scanner stops completely before trading mode."
    )

    fixed.console.print("Testnet fee universe: Y = only Demo 0/0; N = all pairs (fee never blocks Testnet trading)")
    raw_fee_scope = input("Choice [Y/n]: ")
    zero_fee_only = _zero_fee_choice(raw_fee_scope)
    fixed.console.print(
        "Fee filter: "
        + ("ONLY explicit Demo maker=0 / taker=0 pairs" if zero_fee_only else "ALL Binance/MEXC pairs; Demo fee does NOT block Testnet trading")
    )

    rows = await _scan_live_candidates(args, zero_fee_only=zero_fee_only)
    if not rows:
        raise RuntimeError(
            "Fresh scan saw no pair produce a baseline 8bps/3x lead-lag signal in the selected fee universe. "
            "Run again, choose the broader fee universe, or increase --scan-seconds; trading thresholds were not relaxed."
        )

    _show_selectable(rows)
    raw = input("Select pair number or symbol [Enter = #1]: ")
    selected = _select(rows, raw)
    symbol = selected.symbol

    fixed.console.print(
        f"[bold green]SELECTED[/bold green] {symbol} "
        f"signals={selected.candidate.profile.signals} "
        f"median_residual={selected.candidate.profile.median_signal_residual_bps:.1f}bps "
        f"median_strength={selected.candidate.profile.median_signal_strength_ratio:.2f}x "
        f"demo_maker={_fee_bps(selected.maker_fee)} demo_taker={_fee_bps(selected.taker_fee)} "
        f"score={selected.candidate.score:.1f}"
    )
    fixed.console.print(
        "[bold cyan]TRADING MODE[/bold cyan] starts now: scanner feeds are closed; "
        "fee never blocks Testnet trading; no synthetic RTT/fixed sleep; confirmed fill -> immediate management."
    )

    original_symbol = fixed.SYMBOL
    original_gate = fixed.LeadLagGate
    previous_maker = getattr(args, "selected_live_maker_fee_rate", None)
    previous_taker = getattr(args, "selected_live_taker_fee_rate", None)
    fixed.SYMBOL = symbol
    fixed.LeadLagGate = runtime_diag.DiagnosticLeadLagGate
    # These legacy attribute names are consumed only by the Testnet reporting wrapper.
    # Values here are Demo/Testnet fee metadata, not permission for LIVE trading.
    args.selected_live_maker_fee_rate = selected.maker_fee
    args.selected_live_taker_fee_rate = selected.taker_fee
    try:
        await profit_hold.run(args)
    finally:
        fixed.LeadLagGate = original_gate
        fixed.SYMBOL = original_symbol
        args.selected_live_maker_fee_rate = previous_maker
        args.selected_live_taker_fee_rate = previous_taker


def build_parser():
    parser = fixed.build_parser()
    parser.description = "Fresh LIVE Binance/MEXC scan -> interactive pair choice -> MEXC Testnet trading"
    parser.add_argument(
        "--scan-seconds",
        type=float,
        default=30.0,
        help="fresh pre-trade observation window; applies only before trading starts",
    )
    return parser


def main() -> None:
    args = build_parser().parse_args()
    fixed.auto.apply_baseline_v1(args)
    if args.discovery_top <= 0:
        raise SystemExit("--discovery-top must be positive")
    if args.scan_seconds <= 0:
        raise SystemExit("--scan-seconds must be positive")
    try:
        asyncio.run(_run_selected(args))
    except KeyboardInterrupt:
        raise SystemExit(130)
    except Exception as exc:
        fixed.console.print(f"[red]PAIR SELECTOR STOPPED:[/red] {type(exc).__name__}: {exc}")
        raise SystemExit(2)


if __name__ == "__main__":
    main()
