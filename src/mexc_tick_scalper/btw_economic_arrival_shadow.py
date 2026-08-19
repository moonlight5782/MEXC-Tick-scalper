from __future__ import annotations

import asyncio

from . import persistent_end2end_shadow as runner
from .baseline_v1 import apply_baseline_v1
from .prelive_persistent_catchup_shadow import impulse_retention_fraction


def economic_arrival_entry_ok(
    *,
    signal,
    current_residual_bps: float,
    current_binance_price: float,
    current_spread_bps: float,
    min_residual_retention: float,
    min_impulse_retention: float,
    min_remaining_edge_bps: float,
    min_edge_after_spread_bps: float,
):
    """Arrival gate for the BTW real-data shadow.

    Retention ratios are diagnostics only. They must not reject a trade when a
    large absolute residual still survives the measured latency. Hard rejection
    here is limited to a residual direction reversal or too little absolute edge.
    Depth, IOC slippage and full round-trip executable cost are checked by the
    base runner immediately after this function returns True.
    """
    del min_residual_retention, min_impulse_retention

    if signal.direction * current_residual_bps <= 0:
        return False, "residual_reversed", 0.0, 0.0

    residual_retention = abs(current_residual_bps) / max(abs(signal.residual_bps), 1e-12)
    impulse_retention = impulse_retention_fraction(
        signal.direction,
        signal.binance_price,
        signal.binance_move_bps,
        current_binance_price,
    )

    required = max(
        float(min_remaining_edge_bps),
        max(0.0, float(current_spread_bps)) + max(0.0, float(min_edge_after_spread_bps)),
    )
    if abs(float(current_residual_bps)) < required:
        return False, "remaining_edge_too_small", residual_retention, impulse_retention

    return True, "absolute_edge_survived", residual_retention, impulse_retention


async def run(args):
    original = runner.delayed_catchup_entry_ok
    runner.delayed_catchup_entry_ok = economic_arrival_entry_ok
    try:
        return await runner.run(args)
    finally:
        runner.delayed_catchup_entry_ok = original


def main() -> None:
    args = runner.build_parser().parse_args()
    apply_baseline_v1(args)
    if args.target_closed_trades <= 0:
        raise SystemExit("--target-closed-trades must be positive")
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
