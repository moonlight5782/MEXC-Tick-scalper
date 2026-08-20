from __future__ import annotations

import asyncio
import math
import statistics
import time

from ..lead_lag_strategy import LeadLagGate
from ..microspread import MicroSpreadModel
from ..microspread_feed import EventBinanceBookTickerFeed, EventMexcDepthFeed
from ..persistent_lag_profile import PairLagProfile
from .models import CandidateView, RankedCandidate, ScanSignal, ScanStats, TestnetContract
from .snapshot import event_key, valid_snapshot


class LeadLagScanner:
    """Observe public market data and rank pairs; never submit orders."""

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
        trade_entry_hits = 0

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
            trade_entry_hits += int(signal.trade_entry_seen)

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
        candidate = RankedCandidate(
            profile=profile,
            contract=row.contract,
            current_survival=survival,
            score=self._score(profile, survival),
        )
        return CandidateView(
            candidate=candidate,
            trade_entry_hits=trade_entry_hits,
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

        discovery_strength = float(self.args.pair_min_strength_ratio)
        discovery_residual = float(self.args.min_edge_bps)
        self.console.print(
            f"[cyan][3/4][/cyan] Starting public LIVE signal feeds for {len(symbols)} Testnet-compatible pairs."
        )
        self.console.print(
            f"[cyan][3/4][/cyan] Discovery >= {discovery_residual:.1f}bps / {discovery_strength:.2f}x; "
            f"real trading remains >= {self.args.min_absolute_residual_bps:.1f}bps / {self.args.min_signal_strength_ratio:.2f}x."
        )

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
                        snapshot = model.snapshot(now_ms=now_ms, threshold_bps=0.0)
                        if not valid_snapshot(snapshot):
                            continue

                        bucket = stats[symbol]
                        if bucket.active is not None:
                            reason = self._terminal_reason(bucket.active, float(snapshot.edge_bps))
                            if reason is not None:
                                bucket.active.lifetime_ms = max(
                                    0.0,
                                    float(now_ms - bucket.active.started_ms),
                                )
                                bucket.active.terminal_reason = reason
                                bucket.active = None

                        decision = gate.observe(
                            symbol,
                            snapshot,
                            book.spread_bps,
                            now_ms,
                            event_key=event_key(model),
                        )
                        strength = abs(decision.residual_bps) / max(decision.threshold_bps, 1e-12)
                        is_trade_entry = (
                            decision.ready
                            and strength >= self.args.min_signal_strength_ratio
                            and abs(decision.residual_bps) >= self.args.min_absolute_residual_bps
                        )
                        if bucket.active is not None and is_trade_entry:
                            bucket.active.trade_entry_seen = True

                        is_discovery_event = (
                            bucket.active is None
                            and decision.ready
                            and strength >= discovery_strength
                            and abs(decision.residual_bps) >= discovery_residual
                        )
                        if is_discovery_event:
                            signal = ScanSignal(
                                started_ms=now_ms,
                                direction=decision.direction,
                                residual_bps=float(decision.residual_bps),
                                strength=float(strength),
                                trade_entry_seen=bool(is_trade_entry),
                            )
                            bucket.signals.append(signal)
                            bucket.active = signal

                if now >= next_report:
                    discovery_count = sum(len(bucket.signals) for bucket in stats.values())
                    entry_hits = sum(
                        int(signal.trade_entry_seen)
                        for bucket in stats.values()
                        for signal in bucket.signals
                    )
                    pairs = sum(bool(bucket.signals) for bucket in stats.values())
                    phase = "warmup" if now < warmup_until else "sampling"
                    self.console.print(
                        f"[cyan]SCAN[/cyan] {phase} elapsed={now-started:.0f}s "
                        f"discovery_signals={discovery_count} trading_8/3_hits={entry_hits} pairs={pairs}"
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

        total_hits = sum(item.trade_entry_hits for item in candidates)
        self.console.print(
            f"[cyan][4/4][/cyan] Scan complete; discovery candidates={len(candidates)}; "
            f"8/3 hits in shown candidates={total_hits}; scanner feeds stopped."
        )
        return candidates
