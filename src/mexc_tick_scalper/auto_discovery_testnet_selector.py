from __future__ import annotations

import asyncio
from dataclasses import dataclass

from rich.table import Table

from . import auto_discovery_shadow as auto
from . import auto_discovery_testnet_xrp_fixed as fixed
from . import auto_discovery_testnet_xrp_profit_hold as profit_hold
from . import auto_discovery_testnet_xrp_runtime_diag as runtime_diag
from .web_execution import MexcWebExecutionAdapter, WebExecutionConfig


@dataclass(frozen=True, slots=True)
class SelectableCandidate:
    candidate: auto.Candidate
    demo_max_leverage: int

    @property
    def symbol(self) -> str:
        return self.candidate.profile.symbol


async def _testnet_compatible(candidates: list[auto.Candidate]) -> list[SelectableCandidate]:
    """Keep only LIVE Binance/MEXC candidates that also exist on MEXC Testnet."""
    cfg = WebExecutionConfig.demo_from_env(write_enabled=False)
    rows: list[SelectableCandidate] = []
    async with MexcWebExecutionAdapter(cfg) as adapter:
        for candidate in candidates:
            symbol = candidate.profile.symbol
            try:
                detail = await adapter.get_contract_detail(symbol)
            except Exception:
                continue
            contract_size = float(detail.get("contractSize") or 0)
            price_unit = float(detail.get("priceUnit") or 0)
            max_lev = int(detail.get("maxLeverage") or 1)
            if contract_size <= 0 or price_unit <= 0 or max_lev <= 0:
                continue
            rows.append(SelectableCandidate(candidate, max_lev))
    return rows


def _show_selectable(rows: list[SelectableCandidate]) -> None:
    table = Table(title="Binance + MEXC LIVE lead-lag candidates available on MEXC Testnet")
    for col in (
        "#", "Symbol", "Signals", "Med lag", "Survive@RTT", "Residual",
        "Strength", "LIVE lev", "Demo lev", "Score",
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
            f"{candidate.contract.max_leverage}x",
            f"{row.demo_max_leverage}x",
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
        "Scanning strategy-compatible LIVE Binance/MEXC pairs. Scan-time sampling may wait; "
        "after pair selection, trading mode keeps only network/MEXC waits."
    )

    candidates = await auto.discover(args)
    rows = await _testnet_compatible(candidates)
    if not rows:
        raise RuntimeError(
            "No current Binance/MEXC strategy candidate is available as the same symbol on MEXC Testnet"
        )

    rows.sort(key=lambda row: row.candidate.score, reverse=True)
    _show_selectable(rows)

    raw = input("Select pair number or symbol [Enter = #1]: ")
    selected = _select(rows, raw)
    symbol = selected.symbol

    fixed.console.print(
        f"[bold green]SELECTED[/bold green] {symbol} "
        f"score={selected.candidate.score:.1f} "
        f"residual={selected.candidate.profile.median_signal_residual_bps:.1f}bps "
        f"strength={selected.candidate.profile.median_signal_strength_ratio:.2f}x"
    )
    fixed.console.print(
        "[bold cyan]TRADING MODE[/bold cyan] starts now: no synthetic RTT, no fixed sleep, "
        "confirmed fill -> immediate position management."
    )

    # The known-good runner is intentionally left unchanged. Its symbol is a module
    # global, so selection only swaps the instrument before feeds/execution start.
    original_symbol = fixed.SYMBOL
    original_gate = fixed.LeadLagGate
    fixed.SYMBOL = symbol
    fixed.LeadLagGate = runtime_diag.DiagnosticLeadLagGate
    try:
        await profit_hold.run(args)
    finally:
        fixed.LeadLagGate = original_gate
        fixed.SYMBOL = original_symbol


def main() -> None:
    args = fixed.build_parser().parse_args()
    fixed.auto.apply_baseline_v1(args)
    if args.discovery_top <= 0:
        raise SystemExit("--discovery-top must be positive")
    try:
        asyncio.run(_run_selected(args))
    except KeyboardInterrupt:
        raise SystemExit(130)
    except Exception as exc:
        fixed.console.print(f"[red]PAIR SELECTOR STOPPED:[/red] {type(exc).__name__}: {exc}")
        raise SystemExit(2)


if __name__ == "__main__":
    main()
