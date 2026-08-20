from __future__ import annotations

import argparse
import asyncio
import math
import statistics
import time
from dataclasses import dataclass, field
from enum import Enum

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
    lifetime_ms: float | None = None
    terminal_reason: str = ""


@dataclass(slots=True)
class ScanStats:
    signals: list[ScanSignal] = field(default_factory=list)
    active: ScanSignal | None = None


@dataclass(frozen=True, slots=True)
class CandidateView:
    candidate: auto.Candidate
    demo_maker_fee: float | None
    demo_taker_fee: float | None
    demo_max_leverage: int
    metadata_rtt_ms: float

    @property
    def symbol(self) -> str:
        return self.candidate.profile.symbol


class TestnetUniverseService:
    """Builds the Testnet research universe. Never uses private LIVE web auth."""

    def __init__(self, console) -> None:
        self.console = console

    @staticmethod
    def _contract_rows(payload) -> list[dict]:
        data = payload.get("data") if isinstance(payload, dict) else payload
        if isinstance(data, list):
            return [row for row in data if isinstance(row, dict)]
        if isinstance(data, dict):
            if "symbol" in data:
                return [data]
            return [row for row in data.values() if isinstance(row, dict)]
        return []

    async def _public_crosslisted(self) -> list[PublicContract]:
        self.console.print(
            "[cyan][1/4][/cyan] Loading public Binance USD-M + LIVE MEXC contracts; no LIVE web token is used."
        )
        binance_symbols = await fetch_binance_usdm_symbols()
        mexc_rows = await MexcPublicMarket(LIVE_REST, LIVE_WS).contracts()

        rows: list[PublicContract] = []
        for raw in mexc_rows:
            symbol = str(raw.get("symbol") or "").upper()
            if not symbol:
                continue
            binance_symbol = mexc_to_binance_symbol(symbol)
            if binance_symbol not in binance_symbols:
                continue
            contract_size = float(raw.get("contractSize") or 0)
            if contract_size <= 0:
                continue
            rows.append(
                PublicContract(
                    LiveZeroFeeContract(
                        mexc_symbol=symbol,
                        binance_symbol=binance_symbol,
                        max_leverage=int(raw.get("maxLeverage") or 1),
                        contract_size=contract_size,
                        min_vol=float(raw.get("minVol") or 0),
                        maintenance_margin_rate=float(raw.get("maintenanceMarginRate") or 0),
                        initial_margin_rate=float(raw.get("initialMarginRate") or 0),
                        risk_base_vol=float(raw.get("riskBaseVol") or 0),
                        risk_incr_vol=float(raw.get("riskIncrVol") or 0),
                        risk_incr_mmr=float(raw.get("riskIncrMmr") or 0),
                        risk_level_limit=max(1, int(raw.get("riskLevelLimit") or 1)),
                        risk_limit_type=str(raw.get("riskLimitType") or "BY_VOLUME").upper(),
                    )
                )
            )
        rows.sort(key=lambda row: row.symbol)
        self.console.print(f"[cyan][1/4][/cyan] Cross-listed public contracts={len(rows)}")
        return rows

    async def _demo_metadata(self) -> tuple[dict[str, dict], object, float]:
        cfg = WebExecutionConfig.demo_from_env(write_enabled=False)
        async with MexcWebExecutionAdapter(cfg) as adapter:
            started = time.perf_counter_ns()
            payload = await adapter._request("GET", "/contract/detail")
            rtt_ms = (time.perf_counter_ns() - started) / 1_000_000.0
            fee_provider = await read_web_fee_provider(adapter)
        details: dict[str, dict] = {}
        for row in self._contract_rows(payload):
            symbol = str(row.get("symbol") or "").upper()
            if symbol:
                details[symbol] = row
        return details, fee_provider, rtt_ms

    async def load(self, fee_scope: FeeScope) -> list[TestnetContract]:
        public_rows = await self._public_crosslisted()
        if not public_rows:
            raise RuntimeError("No Binance/MEXC cross-listed Futures contracts were found")

        self.console.print(
            f"[cyan][2/4][/cyan] Loading Testnet contract metadata + Demo fee rates for {len(public_rows)} candidates."
        )
        details, fee_provider, rtt_ms = await self._demo_metadata()

        out: list[TestnetContract] = []
        zero_count = 0
        unknown_count = 0
        for public in public_rows:
            detail = details.get(public.symbol)
            if detail is None:
                continue
            if float(detail.get("contractSize") or 0) <= 0 or float(detail.get("priceUnit") or 0) <= 0:
                continue
            max_lev = int(detail.get("maxLeverage") or 0)
            if max_lev <= 0:
                continue

            fee = fee_provider.status(public.symbol)
            maker, taker = fee.maker, fee.taker
            exact_zero = maker == 0.0 and taker == 0.0
            zero_count += int(exact_zero)
            unknown_count += int(maker is None or taker is None)
            if fee_scope is FeeScope.ZERO_ONLY and not exact_zero:
                continue

            out.append(
                TestnetContract(
                    contract=public.contract,
                    demo_maker_fee=maker,
                    demo_taker_fee=taker,
                    demo_max_leverage=max_lev,
                    metadata_rtt_ms=rtt_ms,
                )
            )

        scope_text = "all fees" if fee_scope is FeeScope.ALL else "explicit Demo 0/0 only"
        self.console.print(
            f"[cyan][2/4][/cyan] Testnet contracts={len(details)}; Demo 0/0={zero_count}; "
            f"fee_unknown={unknown_count}; usable={len(out)}; scope={scope_text}; metadata RTT={rtt_ms:.0f}ms"
        )
        return out


class LeadLagScanner:
    """Observes only public market data and returns ranked candidates."""

    def __init__(self, args, console) -> None:
        self.args = args
        self.console = console

    def _terminal_reason(self, signal: ScanSignal, residual_bps: float) -> str | None:
        direction = 1 if residual_bps > 0 else -1 if residual_bps < 0 else 0
        if direction == -signal.direction and abs(residual_bps) >= self.args.reversal_edge_bps:
            return "residual_reversal"
        convergence = max(
            self.args.convergence_bps,
            abs(signal.residual_bps) * self.args.convergence_fraction,
        )
        if abs(residual_bps) <= convergence:
            return "convergence"
        return None

    @staticmethod
    def _score(profile: PairLagProfile, survival: float) -> float:
        evidence = math.log1p(max(0, profile.signals))
        convergence_quality = max(
            0.10,
            profile.convergence_rate + 0.25 * (1.0 - profile.reversal_rate),
        )
        return (
            max(0.0, survival)
            * max(0.0, profile.median_signal_residual_bps)
            * max(0.0, profile.median_signal_strength_ratio)
            * evidence
            * convergence_quality
        )

    def _candidate(self, row: TestnetContract, stats: ScanStats, end_ms: int) -> CandidateView | None:
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
                lifetime = max(0.0, float(end_ms - signal.started_ms))
            lifetimes.append(lifetime)
            residuals.append(abs(signal.residual_bps))
            strengths.append(signal.strength)
            survived += int(lifetime >= row.metadata_rtt_ms)
            convergences += int(signal.terminal_reason == "convergence")
            reversals += int(signal.terminal_reason == "residual_reversal")

        count = len(lifetimes)
        survival = survived / count
        median = lambda values: statistics.median(values) if values else 0.0
        profile = PairLagProfile(
            symbol=row.symbol,
            signals=count,
            median_lifetime_ms=median(lifetimes),
            p75_lifetime_ms=0.0,
            p90_lifetime_ms=0.0,
            survive_execution_rate=survival,
            convergence_rate=convergences / count,
            reversal_rate=reversals / count,
            median_signal_residual_bps=median(residuals),
            median_signal_strength_ratio=median(strengths),
            median_leader_advantage_bps=0.0,
        )
        candidate = auto.Candidate(
            profile=profile,
            contract=row.contract,
            current_survival=survival,
            score=self._score(profile, survival),
        )
        return CandidateView(
            candidate=candidate,
            demo_maker_fee=row.demo_maker_fee,
            demo_taker_fee=row.demo_taker_fee,
            demo_max_leverage=row.demo_max_leverage,
            metadata_rtt_ms=row.metadata_rtt_ms,
        )

    async def scan(self, universe: list[TestnetContract]) -> list[CandidateView]:
        if not universe:
            raise RuntimeError("Testnet universe is empty")

        models = {
            row.symbol: MicroSpreadModel(
                horizon_ms=self.args.micro_horizon_ms,
                baseline_seconds=self.args.baseline_seconds,
                baseline_exclusion_ms=self.args.baseline_exclusion_ms,
                min_edge_bps=0.0,
                min_binance_move_bps=0.0,
                max_binance_age_ms=self.args.max_binance_age_ms,
                max_mexc_age_ms=self.args.max_mexc_age_ms,
            )
            for row in universe
        }
        gate = LeadLagGate(
            noise_window_ms=self.args.noise_window_ms,
            residual_noise_multiplier=self.args.residual_noise_multiplier,
            binance_noise_multiplier=self.args.binance_noise_multiplier,
            min_edge_bps=self.args.min_edge_bps,
            min_net_edge_bps=self.args.min_net_edge_bps,
            spread_ratio=self.args.edge_to_spread_ratio,
            min_binance_move_bps=self.args.min_binance_move_bps,
            min_leader_advantage_bps=self.args.min_leader_advantage_bps,
            min_lead_ratio=self.args.min_lead_ratio,
            confirm_updates=self.args.confirm_updates,
            confirm_ms=self.args.confirm_ms,
            rearm_fraction=self.args.rearm_fraction,
        )

        symbols = [row.symbol for row in universe]
        wake = asyncio.Event()
        binance = EventBinanceBookTickerFeed([row.contract for row in universe], models, wake)
        mexc = EventMexcDepthFeed(symbols, models, wake, depth_limit=self.args.depth_limit)
        stats = {symbol: ScanStats() for symbol in symbols}

        self.console.print(f"[cyan][3/4][/cyan] Starting public LIVE signal feeds for {len(symbols)} Testnet-compatible pairs.")
        await binance.start()
        await mexc.start()
        warmup_until = time.monotonic() + self.args.warmup_seconds
        deadline = warmup_until + self.args.scan_seconds
        started = time.monotonic()
        next_report = started + 5.0
        try:
            while time.monotonic() < deadline:
                now = time.monotonic()
                now_ms = int(time.time() * 1000)
                if now >= warmup_until:
                    for symbol in symbols:
                        book = mexc.books.get(symbol)
                        if book is None or now_ms - book.recv_ms > self.args.max_book_age_ms:
                            continue
                        model = models[symbol]
                        snap = model.snapshot(now_ms=now_ms, threshold_bps=0.0)
                        if not _valid_snapshot(snap):
                            continue
                        bucket = stats[symbol]
                        if bucket.active is not None:
                            reason = self._terminal_reason(bucket.active, float(snap.edge_bps))
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
                            and strength >= self.args.min_signal_strength_ratio
                            and abs(decision.residual_bps) >= self.args.min_absolute_residual_bps
                        ):
                            signal = ScanSignal(
                                started_ms=now_ms,
                                direction=decision.direction,
                                residual_bps=float(decision.residual_bps),
                                strength=float(strength),
                            )
                            bucket.signals.append(signal)
                            bucket.active = signal

                if now >= next_report:
                    signal_count = sum(len(bucket.signals) for bucket in stats.values())
                    pairs = sum(bool(bucket.signals) for bucket in stats.values())
                    phase = "warmup" if now < warmup_until else "sampling"
                    self.console.print(
                        f"[cyan]SCAN[/cyan] {phase} elapsed={now-started:.0f}s qualifying_signals={signal_count} pairs={pairs}"
                    )
                    next_report = now + 5.0

                wake.clear()
                try:
                    await asyncio.wait_for(wake.wait(), timeout=0.25)
                except TimeoutError:
                    pass
        finally:
            await binance.close()
            await mexc.close()

        end_ms = int(time.time() * 1000)
        candidates = [
            candidate
            for row in universe
            if (candidate := self._candidate(row, stats[row.symbol], end_ms)) is not None
        ]
        candidates.sort(key=lambda item: item.candidate.score, reverse=True)
        if self.args.discovery_top > 0:
            candidates = candidates[: self.args.discovery_top]
        self.console.print(f"[cyan][4/4][/cyan] Scan complete; candidates={len(candidates)}; scanner feeds stopped.")
        return candidates


class PairSelector:
    def __init__(self, console) -> None:
        self.console = console

    @staticmethod
    def fee_scope(raw: str) -> FeeScope:
        value = raw.strip().lower()
        if value in {"", "a", "all", "y", "yes", "да", "д"}:
            return FeeScope.ALL
        if value in {"z", "zero", "0", "n", "no", "нет", "н"}:
            return FeeScope.ZERO_ONLY
        raise ValueError("choose All or Zero-only")

    @staticmethod
    def choose(rows: list[CandidateView], raw: str) -> CandidateView:
        if not rows:
            raise ValueError("no selectable candidates")
        value = raw.strip().upper()
        if not value:
            return rows[0]
        if value.isdigit():
            index = int(value)
            if index < 1 or index > len(rows):
                raise ValueError(f"pair number must be between 1 and {len(rows)}")
            return rows[index - 1]
        symbol = value if value.endswith("_USDT") else f"{value}_USDT"
        for row in rows:
            if row.symbol == symbol:
                return row
        raise ValueError(f"{value!r} is not in the current candidate list")

    @staticmethod
    def _fee(value: float | None) -> str:
        return "?" if value is None else f"{value * 10_000.0:.2f}bps"

    def ask_fee_scope(self) -> FeeScope:
        self.console.print("Testnet fee universe: [A]ll pairs (default) or [Z]ero-fee only.")
        return self.fee_scope(input("Choice [A/z]: "))

    def show(self, rows: list[CandidateView]) -> None:
        table = Table(title="Fresh Binance + MEXC scan; executable on MEXC Testnet")
        for column in (
            "#", "Symbol", "Signals", "Med lag", "Survive@RTT", "Residual", "Strength",
            "Demo maker", "Demo taker", "LIVE lev", "Demo lev", "Score",
        ):
            table.add_column(column)
        for index, row in enumerate(rows, 1):
            profile = row.candidate.profile
            table.add_row(
                str(index),
                row.symbol,
                str(profile.signals),
                f"{profile.median_lifetime_ms:.0f}ms",
                f"{row.candidate.current_survival:.0%}",
                f"{profile.median_signal_residual_bps:.1f}bps",
                f"{profile.median_signal_strength_ratio:.2f}x",
                self._fee(row.demo_maker_fee),
                self._fee(row.demo_taker_fee),
                f"{row.candidate.contract.max_leverage}x",
                f"{row.demo_max_leverage}x",
                f"{row.candidate.score:.1f}",
            )
        self.console.print(table)

    def ask_pair(self, rows: list[CandidateView]) -> CandidateView:
        return self.choose(rows, input("Select pair number or symbol [Enter = #1]: "))


class TradingSession:
    """Owns the only compatibility bridge to the frozen trading engine."""

    def __init__(self, args, console) -> None:
        self.args = args
        self.console = console

    async def run(self, selected: CandidateView) -> None:
        self.console.print(
            f"[bold green]SELECTED[/bold green] {selected.symbol} "
            f"Demo maker={PairSelector._fee(selected.demo_maker_fee)} "
            f"Demo taker={PairSelector._fee(selected.demo_taker_fee)}"
        )
        self.console.print(
            "[bold cyan]TRADING MODE[/bold cyan] scanner is stopped; baseline 8bps/3x unchanged; "
            "Demo fees do not block Testnet trading; actual DEMO_FEES/DEMO_NET come from fills."
        )

        # One explicit compatibility bridge. The legacy/frozen engine still exposes
        # symbol and gate class as module-level dependencies. No discovery or auth
        # code is allowed inside this scope.
        previous_symbol = fixed.SYMBOL
        previous_gate = fixed.LeadLagGate
        fixed.SYMBOL = selected.symbol
        fixed.LeadLagGate = runtime_diag.DiagnosticLeadLagGate
        try:
            await profit_hold.run(self.args)
        finally:
            fixed.LeadLagGate = previous_gate
            fixed.SYMBOL = previous_symbol


class TestnetApp:
    def __init__(self, args, console=fixed.console) -> None:
        self.args = args
        self.console = console
        self.universe = TestnetUniverseService(console)
        self.scanner = LeadLagScanner(args, console)
        self.selector = PairSelector(console)
        self.trading = TradingSession(args, console)

    async def run(self) -> None:
        self.console.print("[bold cyan]TESTNET APP[/bold cyan]")
        self.console.print(
            "Responsibilities are isolated: universe -> scan -> selection -> trading. "
            "Testnet never requires MEXC_WEB_TOKEN."
        )
        scope = self.selector.ask_fee_scope()
        universe = await self.universe.load(scope)
        if not universe:
            raise RuntimeError("No Testnet-compatible contracts in the selected fee scope")
        candidates = await self.scanner.scan(universe)
        if not candidates:
            raise RuntimeError(
                "Fresh scan saw no baseline 8bps/3x signal. Increase --scan-seconds or run again; trading thresholds were not relaxed."
            )
        self.selector.show(candidates)
        selected = self.selector.ask_pair(candidates)
        await self.trading.run(selected)


def build_parser() -> argparse.ArgumentParser:
    parser = fixed.build_parser()
    parser.description = "Structured Testnet app: universe -> live scan -> pair selection -> trading"
    parser.add_argument(
        "--scan-seconds",
        type=float,
        default=30.0,
        help="fresh pre-trade observation window; only before trading starts",
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
        asyncio.run(TestnetApp(args).run())
    except KeyboardInterrupt:
        raise SystemExit(130)
    except Exception as exc:
        fixed.console.print(f"[red]TESTNET APP STOPPED:[/red] {type(exc).__name__}: {exc}")
        raise SystemExit(2)


if __name__ == "__main__":
    main()
