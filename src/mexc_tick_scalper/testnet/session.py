from __future__ import annotations

from ..web_execution import WebExecutionConfig
from .models import CandidateView
from .selector import PairSelector
from .trading_engine import TestnetTradingEngine


class TradingSession:
    """Own one selected Testnet trading session; no discovery or global patching."""

    def __init__(self, args, execution_config: WebExecutionConfig, console) -> None:
        self.args = args
        self.execution_config = execution_config
        self.console = console

    async def run(self, selected: CandidateView) -> None:
        self.console.print(
            f"[bold green]SELECTED[/bold green] {selected.symbol} "
            f"discovery={selected.candidate.profile.signals} 8/3_hits={selected.trade_entry_hits} "
            f"Demo maker={PairSelector.fee_text(selected.demo_maker_fee)} "
            f"Demo taker={PairSelector.fee_text(selected.demo_taker_fee)}"
        )
        self.console.print(
            "[bold cyan]TRADING MODE[/bold cyan] discovery feeds are stopped; "
            "real entry remains baseline 8bps/3x; Demo fees are reported but do not block ALL-mode testing."
        )
        engine = TestnetTradingEngine(
            args=self.args,
            selected=selected,
            execution_config=self.execution_config,
            console=self.console,
        )
        await engine.run()
