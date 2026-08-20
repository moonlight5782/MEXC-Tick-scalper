from __future__ import annotations

import csv
import math
from dataclasses import dataclass, field
from pathlib import Path

from ..execution import OrderFill
from .risk import BankState


@dataclass(slots=True)
class SessionStats:
    signals: int = 0
    entries: int = 0
    expired: int = 0
    nofill: int = 0
    wins: int = 0
    losses: int = 0
    flats: int = 0
    gross_pnl_usdt: float = 0.0
    demo_fees_usdt: float = 0.0
    demo_net_pnl_usdt: float = 0.0
    gross_wins: float = 0.0
    gross_losses: float = 0.0
    fills: list[float] = field(default_factory=list)
    holds_ms: list[float] = field(default_factory=list)
    signal_to_fill_ms: list[float] = field(default_factory=list)
    exits: dict[str, int] = field(default_factory=dict)

    @property
    def closed(self) -> int:
        return self.wins + self.losses + self.flats

    @property
    def profit_factor(self) -> float:
        if self.gross_losses <= 0:
            return math.inf if self.gross_wins > 0 else 0.0
        return self.gross_wins / self.gross_losses


def _median(values: list[float]) -> float:
    if not values:
        return 0.0
    rows = sorted(values)
    middle = len(rows) // 2
    if len(rows) % 2:
        return rows[middle]
    return (rows[middle - 1] + rows[middle]) / 2.0


class TradeReporter:
    FIELDS = [
        "signal_ms",
        "entry_ms",
        "management_start_ms",
        "exit_decision_ms",
        "exit_submit_ms",
        "exit_ms",
        "symbol",
        "direction",
        "requested_notional_usdt",
        "filled_notional_usdt",
        "fill_ratio",
        "leverage",
        "actual_margin_usdt",
        "demo_entry_best",
        "entry_price",
        "exit_price",
        "entry_slippage_bps",
        "gross_pnl_bps",
        "gross_pnl_usdt",
        "entry_fee_usdt",
        "exit_fee_usdt",
        "demo_fees_usdt",
        "demo_net_pnl_usdt",
        "gross_roe_pct",
        "mfe_bps",
        "mae_bps",
        "hold_ms",
        "signal_to_submit_ms",
        "submit_to_fill_ms",
        "signal_to_fill_ms",
        "fill_to_management_ms",
        "exit_decision_to_submit_ms",
        "exit_submit_to_fill_ms",
        "exit_reason",
        "profit_hold_armed",
        "entry_order_id",
        "exit_order_id",
    ]

    def __init__(self, path: Path, console) -> None:
        self.path = path
        self.console = console

    def prepare(self, *, append: bool) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists() and not append:
            self.path.unlink()

    def append(self, row: dict[str, object]) -> None:
        exists = self.path.exists()
        with self.path.open("a", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=self.FIELDS)
            if not exists:
                writer.writeheader()
            writer.writerow({field: row.get(field, "") for field in self.FIELDS})

    def record_close(
        self,
        *,
        stats: SessionStats,
        bank: BankState,
        direction: int,
        qty: float,
        entry_price: float,
        filled_notional: float,
        requested_notional: float,
        leverage: int,
        demo_entry_best: float,
        entry_slippage_bps: float,
        entry_fill: OrderFill,
        exit_fill: OrderFill,
        signal_ms: int,
        entry_ms: int,
        management_start_ms: int,
        exit_decision_ms: int,
        exit_submit_ms: float,
        exit_done_ms: float,
        mfe_bps: float,
        mae_bps: float,
        reason: str,
        profit_hold_armed: bool,
        entry_submit_ms: float,
    ) -> tuple[float, float]:
        exit_price = float(exit_fill.avg_price or entry_price)
        gross = direction * (exit_price - entry_price) * qty
        fees = float(entry_fill.fee_usdt) + float(exit_fill.fee_usdt)
        net = gross - fees
        gross_bps = gross / max(filled_notional, 1e-12) * 10_000.0
        margin = filled_notional / max(float(leverage), 1.0)
        gross_roe = gross / max(margin, 1e-12) * 100.0
        hold_ms = max(0, int(exit_done_ms) - entry_ms)

        before = bank.balance_usdt
        bank.balance_usdt = max(0.0, bank.balance_usdt + gross)
        stats.gross_pnl_usdt += gross
        stats.demo_fees_usdt += fees
        stats.demo_net_pnl_usdt += net
        stats.holds_ms.append(float(hold_ms))
        stats.exits[reason] = stats.exits.get(reason, 0) + 1
        if gross > 1e-9:
            stats.wins += 1
            stats.gross_wins += gross
        elif gross < -1e-9:
            stats.losses += 1
            stats.gross_losses += abs(gross)
        else:
            stats.flats += 1

        self.append({
            "signal_ms": signal_ms,
            "entry_ms": entry_ms,
            "management_start_ms": management_start_ms,
            "exit_decision_ms": exit_decision_ms,
            "exit_submit_ms": exit_submit_ms,
            "exit_ms": int(exit_done_ms),
            "symbol": exit_fill.symbol,
            "direction": "LONG" if direction > 0 else "SHORT",
            "requested_notional_usdt": requested_notional,
            "filled_notional_usdt": filled_notional,
            "fill_ratio": filled_notional / max(requested_notional, 1e-12),
            "leverage": leverage,
            "actual_margin_usdt": margin,
            "demo_entry_best": demo_entry_best,
            "entry_price": entry_price,
            "exit_price": exit_price,
            "entry_slippage_bps": entry_slippage_bps,
            "gross_pnl_bps": gross_bps,
            "gross_pnl_usdt": gross,
            "entry_fee_usdt": entry_fill.fee_usdt,
            "exit_fee_usdt": exit_fill.fee_usdt,
            "demo_fees_usdt": fees,
            "demo_net_pnl_usdt": net,
            "gross_roe_pct": gross_roe,
            "mfe_bps": mfe_bps,
            "mae_bps": mae_bps,
            "hold_ms": hold_ms,
            "signal_to_submit_ms": entry_submit_ms - signal_ms,
            "submit_to_fill_ms": entry_ms - entry_submit_ms,
            "signal_to_fill_ms": entry_ms - signal_ms,
            "fill_to_management_ms": management_start_ms - entry_ms,
            "exit_decision_to_submit_ms": exit_submit_ms - exit_decision_ms,
            "exit_submit_to_fill_ms": exit_done_ms - exit_submit_ms,
            "exit_reason": reason,
            "profit_hold_armed": profit_hold_armed,
            "entry_order_id": entry_fill.order_id,
            "exit_order_id": exit_fill.order_id,
        })

        self.console.print(
            f"[{'green' if gross > 0 else 'red'}]TESTNET EXIT[/] {exit_fill.symbol} reason={reason} "
            f"GROSS={gross_bps:+.2f}bps ${gross:+.2f} DEMO_FEES=${fees:.4f} "
            f"DEMO_NET=${net:+.2f} hold={hold_ms}ms logical_bank=${before:.2f}->${bank.balance_usdt:.2f}"
        )
        return gross, net

    @staticmethod
    def summary(stats: SessionStats, bank: BankState) -> str:
        closed = stats.closed
        win_rate = stats.wins / closed * 100.0 if closed else 0.0
        pf = "inf" if math.isinf(stats.profit_factor) else f"{stats.profit_factor:.3f}"
        exits = ",".join(f"{key}:{value}" for key, value in sorted(stats.exits.items())) or "-"
        return (
            f"signals={stats.signals} entries={stats.entries} expired={stats.expired} nofill={stats.nofill} "
            f"W/L/F={stats.wins}/{stats.losses}/{stats.flats} WR={win_rate:.1f}% PF_GROSS={pf} "
            f"gross={stats.gross_pnl_usdt:+.4f}USDT demo_fees=${stats.demo_fees_usdt:.4f} "
            f"demo_net={stats.demo_net_pnl_usdt:+.4f}USDT logical_bank=${bank.balance_usdt:.2f} "
            f"fill_med={_median(stats.fills)*100:.1f}% hold_med={_median(stats.holds_ms):.0f}ms "
            f"signal_to_fill_med={_median(stats.signal_to_fill_ms):.0f}ms exits={exits}"
        )
