from __future__ import annotations

import asyncio

from . import auto_discovery_shadow as base


_ACTIVE_ARGS = None
_ORIGINAL_AUTO_FILL = base._auto_sized_virtual_ioc_fill


def _tracked_auto_fill(
    book, *, direction: int, target_notional_usdt: float,
    contract_size: float, cross_bps: float,
):
    """Keep the core runner's reporting target synchronized with the actual risk-sized IOC request.

    The base risk wrapper dynamically caps the historical $10k target by LIVE leverage
    and current equity reserve. The core runner logs/writes args.target_notional_usdt,
    so without this synchronization it would misleadingly report $10k even when the
    actual IOC request was, for example, $4k or $1.8k.

    There is only one pending/open position at a time in the core runner, therefore
    the value remains the correct request for that position until it is closed.
    """
    del target_notional_usdt
    requested, _, _ = base._requested_notional_and_margin(base.CURRENT_SYMBOL)
    if _ACTIVE_ARGS is not None:
        _ACTIVE_ARGS.target_notional_usdt = requested
    return _ORIGINAL_AUTO_FILL(
        book,
        direction=direction,
        target_notional_usdt=requested,
        contract_size=contract_size,
        cross_bps=cross_bps,
    )


async def run(args):
    global _ACTIVE_ARGS
    _ACTIVE_ARGS = args
    original = base._auto_sized_virtual_ioc_fill
    base._auto_sized_virtual_ioc_fill = _tracked_auto_fill
    try:
        return await base.run(args)
    finally:
        base._auto_sized_virtual_ioc_fill = original
        _ACTIVE_ARGS = None


def main() -> None:
    args = base.build_parser().parse_args()
    base.apply_baseline_v1(args)
    if args.discovery_top <= 0:
        raise SystemExit("--discovery-top must be positive")
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
