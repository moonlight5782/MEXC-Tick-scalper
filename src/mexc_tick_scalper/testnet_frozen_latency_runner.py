from __future__ import annotations

import asyncio
from pathlib import Path

from rich.console import Console

from .baseline_v1 import BASELINE_V1, apply_baseline_v1
from .execution import OrderSide
from .live_zero_fee_universe import discover_live_zero_fee_crosslisted
from .persistent_lag_profile import build_profiles, latest_lifetime_csv, select_profiles
from .testnet_known_good_v1 import _testnet_contract_rows
from . import testnet_known_good_risk as risk
from .web_execution import MexcWebError, MexcWebExecutionAdapter

console = Console()
REFERENCE_COMMIT = "8a0bc6043385dbaf95ec8e77b93d91fd00a7f9e5"
_LIFETIME_CSV = ""


async def _frozen_execution_universe(
    adapter: MexcWebExecutionAdapter,
) -> tuple[list, dict[str, dict]]:
    """Original frozen production eligibility, intersected with Testnet only at execution boundary."""
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
                adapter.get_best_price(symbol, OrderSide.LONG),
                adapter.get_best_price(symbol, OrderSide.SHORT),
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
    if not selected:
        raise MexcWebError(
            "No frozen-baseline eligible pair is currently executable on Testnet. "
            "Do not weaken strategy gates; retry when Testnet exposes an eligible symbol/book."
        )
    return selected, details


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
    console.print(f"[bold]FROZEN LATENCY REFERENCE[/bold] {REFERENCE_COMMIT}")
    console.print(
        "BASELINE_V1 is forcibly applied. Persistent profile + LIVE exact 0/0 fee eligibility + Binance cross-listing "
        "remain unchanged; Testnet availability is only the final execution intersection."
    )
    try:
        asyncio.run(risk.run(args))
    except MexcWebError as exc:
        console.print(f"[red]FROZEN TESTNET RUN FAILED:[/red] {exc}")
        raise SystemExit(2) from exc


if __name__ == "__main__":
    main()
