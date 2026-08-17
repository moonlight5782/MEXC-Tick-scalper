from __future__ import annotations

from . import demo_live_baseline_execution_proxy as base
from .web_execution import MexcWebError


class StrictDemoExecutionProxy(base.DemoExecutionProxy):
    async def entry(self, signal_symbol: str, side_text: str, paper_filled_notional: float) -> None:
        entries_before = self.entries
        skipped_before = self.skipped
        await super().entry(signal_symbol, side_text, paper_filled_notional)
        if self.entries == entries_before:
            reason = "Testnet IOC was not opened"
            if self.skipped > skipped_before:
                reason += "; the Demo execution path rejected or failed the entry"
            raise MexcWebError(
                f"{reason} for signal={signal_symbol} proxy={self.demo_symbol}. "
                "Stopping instead of continuing with paper-only results."
            )


def main() -> None:
    base.DemoExecutionProxy = StrictDemoExecutionProxy
    base.main()


if __name__ == "__main__":
    main()
