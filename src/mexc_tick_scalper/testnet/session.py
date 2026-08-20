from __future__ import annotations

from .. import auto_discovery_testnet_xrp_fixed as legacy_engine
from .. import auto_discovery_testnet_xrp_profit_hold as profit_hold
from .. import auto_discovery_testnet_xrp_runtime_diag as runtime_diag
from .models import CandidateView
from .selector import PairSelector


class TradingSession:
    """Trade one selected pair.

    This is the single temporary compatibility bridge to the legacy monolithic
    Testnet engine. Discovery/configuration/selection must never import that engine.
    Remove this bridge when execution and position management are fully extracted.
    """

    def __init__(self, args, console) -> None:
        self.args = args
        self.console = console

    async def run(self, selected: CandidateView) -> None:
        self.console.print(
            f"[bold green]SELECTED[/bold green] {selected.symbol} "
            f"discovery={selected.candidate.profile.signals} 8/3_hits={selected.trade_entry_hits} "
            f"Demo maker={PairSelector.fee_text(selected.demo_maker_fee)} "
            f"Demo taker={PairSelector.fee_text(selected.demo_taker_fee)}"
        )
        self.console.print(
            "[bold cyan]TRADING MODE[/bold cyan] scanner is stopped; baseline 8bps/3x unchanged; "
            "Demo fees do not block Testnet trading; actual DEMO_FEES/DEMO_NET come from fills."
        )

        previous_symbol = legacy_engine.SYMBOL
        previous_gate = legacy_engine.LeadLagGate
        legacy_engine.SYMBOL = selected.symbol
        legacy_engine.LeadLagGate = runtime_diag.DiagnosticLeadLagGate
        try:
            await profit_hold.run(self.args)
        finally:
            legacy_engine.LeadLagGate = previous_gate
            legacy_engine.SYMBOL = previous_symbol
