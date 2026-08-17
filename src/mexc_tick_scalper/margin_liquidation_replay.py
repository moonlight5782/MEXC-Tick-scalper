from __future__ import annotations

import argparse
import asyncio
import math
import re
from dataclasses import dataclass
from pathlib import Path

import aiohttp
from rich.console import Console

console = Console()

CONTRACT_DETAIL_URL = "https://contract.mexc.com/api/v1/contract/detail"

ENTRY_RE = re.compile(
    r"ENTRY\s+(?P<symbol>[A-Z0-9_]+)\s+(?P<side>LONG|SHORT).*?filled=\$(?P<notional>[0-9.]+)"
)
EXIT_RE = re.compile(
    r"EXIT\s+(?P<symbol>[A-Z0-9_]+)\s+(?P<reason>[a-zA-Z0-9_]+)\s+"
    r"pnl=(?P<pnl_bps>[+-]?[0-9.]+)bps\s+\$(?P<pnl_usdt>[+-]?[0-9.]+)"
)


@dataclass(frozen=True, slots=True)
class ContractRisk:
    symbol: str
    max_leverage: int
    maintenance_margin_rate: float


@dataclass(frozen=True, slots=True)
class LoggedTrade:
    symbol: str
    direction: int
    recorded_notional: float
    exit_reason: str
    exit_pnl_bps: float


@dataclass(frozen=True, slots=True)
class ReplayTrade:
    index: int
    symbol: str
    direction: int
    leverage: int
    mmr: float
    notional: float
    initial_margin: float
    liquidation_distance_bps: float
    exit_pnl_bps: float
    liquidated_by_exit_lower_bound: bool
    pnl_usdt: float
    balance_after: float


def liquidation_distance_bps(leverage: int, maintenance_margin_rate: float, liquidation_fee_rate: float = 0.0) -> float:
    """Approximate isolated-margin liquidation distance for linear USDT futures.

    MEXC's published isolated formula is Position Margin + unrealized PnL <=
    Maintenance Margin (+ liquidation fee). With position margin = notional/leverage,
    the distance from entry is approximately 1/leverage - MMR - liquidation fee rate.
    """
    if leverage <= 0:
        raise ValueError("leverage must be positive")
    distance = (1.0 / float(leverage)) - float(maintenance_margin_rate) - float(liquidation_fee_rate)
    return max(0.0, distance * 10_000.0)


def liquidation_price(entry_price: float, direction: int, leverage: int, maintenance_margin_rate: float, liquidation_fee_rate: float = 0.0) -> float:
    if entry_price <= 0:
        raise ValueError("entry_price must be positive")
    distance = liquidation_distance_bps(leverage, maintenance_margin_rate, liquidation_fee_rate) / 10_000.0
    return entry_price * (1.0 - distance if direction > 0 else 1.0 + distance)


def parse_log(text: str) -> list[LoggedTrade]:
    open_trade: tuple[str, int, float] | None = None
    out: list[LoggedTrade] = []
    for raw in text.splitlines():
        m = ENTRY_RE.search(raw)
        if m:
            open_trade = (
                m.group("symbol"),
                1 if m.group("side") == "LONG" else -1,
                float(m.group("notional")),
            )
            continue
        m = EXIT_RE.search(raw)
        if not m or open_trade is None:
            continue
        symbol, direction, notional = open_trade
        if m.group("symbol") != symbol:
            continue
        out.append(
            LoggedTrade(
                symbol=symbol,
                direction=direction,
                recorded_notional=notional,
                exit_reason=m.group("reason"),
                exit_pnl_bps=float(m.group("pnl_bps")),
            )
        )
        open_trade = None
    return out


async def fetch_contract_risk() -> dict[str, ContractRisk]:
    timeout = aiohttp.ClientTimeout(total=15)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(CONTRACT_DETAIL_URL) as response:
            response.raise_for_status()
            payload = await response.json()
    rows = payload.get("data") or []
    out: dict[str, ContractRisk] = {}
    for row in rows:
        symbol = str(row.get("symbol") or "").upper()
        if not symbol:
            continue
        out[symbol] = ContractRisk(
            symbol=symbol,
            max_leverage=max(1, int(row.get("maxLeverage") or 1)),
            maintenance_margin_rate=max(0.0, float(row.get("maintenanceMarginRate") or 0.0)),
        )
    return out


def replay(
    trades: list[LoggedTrade],
    risks: dict[str, ContractRisk],
    *,
    starting_balance_usdt: float,
    initial_margin_usdt: float,
    requested_leverage: int = 0,
    liquidation_fee_rate: float = 0.0,
) -> list[ReplayTrade]:
    if starting_balance_usdt <= 0:
        raise ValueError("starting balance must be positive")
    if initial_margin_usdt <= 0:
        raise ValueError("initial margin must be positive")

    balance = float(starting_balance_usdt)
    out: list[ReplayTrade] = []
    for index, trade in enumerate(trades, start=1):
        if balance <= 0:
            break
        risk = risks.get(trade.symbol)
        if risk is None:
            raise KeyError(f"Missing MEXC contract risk data for {trade.symbol}")

        leverage = risk.max_leverage if requested_leverage <= 0 else min(requested_leverage, risk.max_leverage)
        leverage = max(1, leverage)
        margin_budget = min(float(initial_margin_usdt), balance)
        max_notional = margin_budget * leverage
        notional = min(trade.recorded_notional, max_notional)
        position_margin = notional / leverage

        liq_bps = liquidation_distance_bps(leverage, risk.maintenance_margin_rate, liquidation_fee_rate)
        liquidated = trade.exit_pnl_bps <= -liq_bps + 1e-12
        if liquidated:
            pnl = -notional * liq_bps / 10_000.0
        else:
            pnl = notional * trade.exit_pnl_bps / 10_000.0
        balance = max(0.0, balance + pnl)

        out.append(
            ReplayTrade(
                index=index,
                symbol=trade.symbol,
                direction=trade.direction,
                leverage=leverage,
                mmr=risk.maintenance_margin_rate,
                notional=notional,
                initial_margin=position_margin,
                liquidation_distance_bps=liq_bps,
                exit_pnl_bps=trade.exit_pnl_bps,
                liquidated_by_exit_lower_bound=liquidated,
                pnl_usdt=pnl,
                balance_after=balance,
            )
        )
    return out


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Fast replay of an existing PAPER log with account balance, isolated margin, leverage and liquidation constraints"
    )
    p.add_argument("log", type=Path, help="Text log produced by the LIVE PAPER runner")
    p.add_argument("--balance-usdt", type=float, default=100.0)
    p.add_argument("--initial-margin-usdt", type=float, default=50.0)
    p.add_argument("--leverage", type=int, default=0, help="0 = current MEXC max leverage for each contract")
    p.add_argument("--liquidation-fee-rate", type=float, default=0.0)
    return p


async def _main_async(args: argparse.Namespace) -> None:
    text = args.log.read_text(encoding="utf-8", errors="replace")
    trades = parse_log(text)
    if not trades:
        raise RuntimeError("No completed ENTRY/EXIT trades found in the log")
    risks = await fetch_contract_risk()
    rows = replay(
        trades,
        risks,
        starting_balance_usdt=args.balance_usdt,
        initial_margin_usdt=args.initial_margin_usdt,
        requested_leverage=args.leverage,
        liquidation_fee_rate=args.liquidation_fee_rate,
    )

    console.print(
        f"[bold cyan]MARGIN/LIQUIDATION REPLAY[/bold cyan] trades={len(rows)} start=${args.balance_usdt:.2f} "
        f"margin_budget=${args.initial_margin_usdt:.2f} leverage={'MEXC_MAX' if args.leverage <= 0 else str(args.leverage)+'x'}"
    )
    liquidations = 0
    gross_win = 0.0
    gross_loss = 0.0
    for row in rows:
        liquidations += int(row.liquidated_by_exit_lower_bound)
        gross_win += max(0.0, row.pnl_usdt)
        gross_loss += max(0.0, -row.pnl_usdt)
        flag = " LIQUIDATED*" if row.liquidated_by_exit_lower_bound else ""
        console.print(
            f"#{row.index:03d} {row.symbol} {'LONG' if row.direction > 0 else 'SHORT'} {row.leverage}x "
            f"margin=${row.initial_margin:.2f} notional=${row.notional:.0f} MMR={row.mmr:.4%} "
            f"liq_dist={row.liquidation_distance_bps:.2f}bps exit={row.exit_pnl_bps:+.2f}bps "
            f"pnl=${row.pnl_usdt:+.2f} balance=${row.balance_after:.2f}{flag}"
        )

    final_balance = rows[-1].balance_after if rows else args.balance_usdt
    pf = math.inf if gross_loss == 0 and gross_win > 0 else gross_win / gross_loss if gross_loss > 0 else 0.0
    pf_text = "inf" if math.isinf(pf) else f"{pf:.3f}"
    console.print(
        f"\n[bold]FINAL[/bold] balance=${final_balance:.2f} pnl=${final_balance-args.balance_usdt:+.2f} "
        f"PF={pf_text} lower_bound_liquidations={liquidations}/{len(rows)}"
    )
    console.print(
        "[yellow]* Liquidation detection is a LOWER BOUND when replaying old logs: the log has entry/exit PnL but not the full "
        "intratrade fair-price path. A winning trade could have crossed liquidation first and recovered; this replay cannot see that. "
        "The next LIVE paper runner should track fair/mark price continuously.[/yellow]"
    )


def main() -> None:
    asyncio.run(_main_async(build_parser().parse_args()))


if __name__ == "__main__":
    main()
