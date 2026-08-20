from __future__ import annotations

from dataclasses import dataclass

from .profit_hold import ProfitHoldPolicy


@dataclass(frozen=True, slots=True)
class ExitContext:
    age_ms: int
    mid_move_bps: float
    leader_move_bps: float
    residual_bps: float
    signal_direction: int
    entry_residual_bps: float
    executable_pnl_bps: float


@dataclass(frozen=True, slots=True)
class TestnetExitPolicy:
    """Evaluate exits without mutating strategy configuration.

    The Testnet execution policy intentionally reacts immediately to adverse moves:
    there is no artificial minimum hold and the emergency mid cut is 0.01 bps.
    Once Profit Hold is armed, ordinary thesis/lifecycle exits are suppressed and
    the positive trailing stop owns the normal winner exit.
    """

    emergency_mid_adverse_bps: float = 0.01
    min_hold_ms: int = 0

    def evaluate(self, context: ExitContext, args, profit_hold: ProfitHoldPolicy) -> str | None:
        if context.age_ms < self.min_hold_ms:
            return None

        # Hard protection always remains active, including after Profit Hold arms.
        if context.mid_move_bps <= -self.emergency_mid_adverse_bps:
            return "mid_adverse_cut"

        if profit_hold.ordinary_thesis_exits_allowed:
            if context.leader_move_bps <= -args.leader_retrace_exit_bps:
                return "leader_retrace"

            residual_direction = (
                1 if context.residual_bps > 0 else -1 if context.residual_bps < 0 else 0
            )
            if (
                residual_direction == -context.signal_direction
                and abs(context.residual_bps) >= args.reversal_edge_bps
            ):
                return "residual_reversal"

            convergence = max(
                args.convergence_bps,
                abs(context.entry_residual_bps) * args.convergence_fraction,
            )
            if (
                abs(context.residual_bps) <= convergence
                and context.mid_move_bps >= args.min_catchup_bps
            ):
                return "mexc_catchup_convergence"

            if context.age_ms >= args.no_progress_ms and context.mid_move_bps < args.min_progress_bps:
                return "no_progress"

        trail = profit_hold.stop_bps
        if trail is not None and context.executable_pnl_bps <= trail:
            return "positive_trailing_stop"

        if profit_hold.ordinary_thesis_exits_allowed and context.age_ms >= args.max_hold_ms:
            return "timeout"

        return None
