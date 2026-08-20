from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR

from ..execution import OrderSide
from ..web_execution import MexcWebError


START_BANK_USDT = 100.0
REQUESTED_LEVERAGE = 200.0
TARGET_NOTIONAL_USDT = 10_000.0
MIN_EQUITY_RESERVE_FRACTION = 0.20
MAX_SESSION_DRAWDOWN_FRACTION = 0.60


@dataclass(slots=True)
class BankState:
    balance_usdt: float = START_BANK_USDT

    @property
    def drawdown_stop_balance(self) -> float:
        return START_BANK_USDT * (1.0 - MAX_SESSION_DRAWDOWN_FRACTION)

    @property
    def max_allocatable_margin_usdt(self) -> float:
        return max(0.0, self.balance_usdt) * (1.0 - MIN_EQUITY_RESERVE_FRACTION)

    @property
    def may_open_new_position(self) -> bool:
        return (
            self.balance_usdt > self.drawdown_stop_balance
            and self.max_allocatable_margin_usdt > 0.0
        )


def effective_leverage(live_max: int, demo_max: int) -> int:
    return max(1, min(int(REQUESTED_LEVERAGE), max(1, int(live_max)), max(1, int(demo_max))))


def requested_notional(bank: BankState, leverage: float) -> tuple[float, float, float]:
    leverage = max(1.0, float(leverage))
    required_margin = TARGET_NOTIONAL_USDT / leverage
    margin = min(required_margin, bank.max_allocatable_margin_usdt)
    requested = margin * leverage
    reserve = max(0.0, bank.balance_usdt - margin)
    return requested, margin, reserve


def demo_ioc_price(best: float, side: OrderSide, cross_bps: float, price_unit: float) -> float:
    if best <= 0 or price_unit <= 0:
        raise MexcWebError("invalid Testnet best price or priceUnit")
    cross = Decimal(str(max(0.0, cross_bps))) / Decimal("10000")
    factor = Decimal("1") + cross if side is OrderSide.LONG else Decimal("1") - cross
    raw = Decimal(str(best)) * factor
    tick = Decimal(str(price_unit))
    rounding = ROUND_CEILING if side is OrderSide.LONG else ROUND_FLOOR
    return float((raw / tick).to_integral_value(rounding=rounding) * tick)
