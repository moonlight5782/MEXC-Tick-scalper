from __future__ import annotations

from rich.table import Table

from .models import CandidateView, FeeScope


class PairSelector:
    """Console-only Testnet fee/pair selection. No network or strategy side effects."""

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
    def fee_text(value: float | None) -> str:
        return "?" if value is None else f"{value * 10_000.0:.2f}bps"

    def ask_fee_scope(self) -> FeeScope:
        self.console.print("Testnet fee universe: [A]ll pairs (default) or [Z]ero-fee only.")
        self.console.print("Choice [A/z]: ", end="")
        return self.fee_scope(input())

    def show(self, rows: list[CandidateView]) -> None:
        table = Table(title="Fresh Binance + MEXC discovery; executable on MEXC Testnet")
        for column in (
            "#",
            "Symbol",
            "Discovery",
            "8/3 hits",
            "Med lag",
            "Survive@RTT",
            "Residual",
            "Strength",
            "Demo maker",
            "Demo taker",
            "LIVE lev",
            "Demo lev",
            "Score",
        ):
            table.add_column(column)

        for index, row in enumerate(rows, 1):
            profile = row.candidate.profile
            table.add_row(
                str(index),
                row.symbol,
                str(profile.signals),
                str(row.trade_entry_hits),
                f"{profile.median_lifetime_ms:.0f}ms",
                f"{row.candidate.current_survival:.0%}",
                f"{profile.median_signal_residual_bps:.1f}bps",
                f"{profile.median_signal_strength_ratio:.2f}x",
                self.fee_text(row.demo_maker_fee),
                self.fee_text(row.demo_taker_fee),
                f"{row.candidate.contract.max_leverage}x",
                f"{row.demo_max_leverage}x",
                f"{row.candidate.score:.1f}",
            )
        self.console.print(table)

    def ask_pair(self, rows: list[CandidateView]) -> CandidateView:
        self.console.print("Select pair number or symbol [Enter = #1]: ", end="")
        return self.choose(rows, input())
