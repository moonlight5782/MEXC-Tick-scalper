from __future__ import annotations

import argparse
import asyncio
import os
import re
import sys
import time
from dataclasses import dataclass

from rich.console import Console

from . import demo_hybrid_test as demo
from .demo_activity import sample_many
from .demo_discovery import _fetch_contracts
from .demo_smoke import _assert_demo_safety
from .demo_tick_test import _trade_pnl
from .execution import OrderSide
from .lead_lag import fetch_binance_usdm_symbols, mexc_to_binance_symbol
from .market import MexcPublicMarket
from .web_execution import MexcWebError, MexcWebExecutionAdapter, WebExecutionConfig

console = Console()
LIVE_REST = "https://contract.mexc.com"
LIVE_WS = "wss://contract.mexc.com/edge"

ENTRY_RE = re.compile(
    r"\bENTRY\s+(?P<symbol>[A-Z0-9_]+)\s+(?P<side>LONG|SHORT)\s+"
    r"requested=\$(?P<requested>[0-9.]+)\s+filled=\$(?P<filled>[0-9.]+)"
)
EXIT_RE = re.compile(r"\bEXIT\s+(?P<symbol>[A-Z0-9_]+)\s+(?P<reason>[A-Za-z0-9_]+)")


@dataclass(slots=True)
class MirroredPosition:
    symbol: str
    side: OrderSide
    entry_price: float
    entry_fee_usdt: float
    leverage: int
    qty: float
    entry_time: float


async def _usable_demo_symbols(adapter: MexcWebExecutionAdapter) -> list[str]:
    contracts = await _fetch_contracts(adapter)
    usable: list[str] = []
    for row in contracts:
        symbol = str(row.get("symbol") or "").upper()
        if not symbol:
            continue
        try:
            ask, bid = await asyncio.gather(
                adapter.get_best_price(symbol, OrderSide.LONG),
                adapter.get_best_price(symbol, OrderSide.SHORT),
            )
        except MexcWebError:
            continue
        if ask > 0 and bid > 0 and ask >= bid:
            usable.append(symbol)
    return sorted(set(usable))


async def _select_demo_test_symbol(adapter: MexcWebExecutionAdapter, requested: str = "") -> str:
    demo_symbols = await _usable_demo_symbols(adapter)
    if not demo_symbols:
        raise MexcWebError("Testnet has no usable contracts with a two-sided book")

    binance_symbols = await fetch_binance_usdm_symbols()
    live_rows = await MexcPublicMarket(LIVE_REST, LIVE_WS).contracts()
    live_symbols = {str(row.get("symbol") or "").upper() for row in live_rows}
    candidates = [
        symbol for symbol in demo_symbols
        if symbol in live_symbols and mexc_to_binance_symbol(symbol) in binance_symbols
    ]
    if requested:
        wanted = requested.upper()
        if wanted not in candidates:
            raise MexcWebError(
                f"requested Demo symbol {wanted} must exist on Testnet + LIVE MEXC and have a Binance USD-M leader"
            )
        return wanted
    if not candidates:
        raise MexcWebError("no Testnet symbol is also available on LIVE MEXC + Binance USD-M")

    console.print(
        f"[cyan]DEMO PAIR SELECTION[/cyan] {len(candidates)} Testnet contracts can use the same Binance->MEXC signal. "
        "Measuring Testnet activity; Demo fee does NOT filter candidates."
    )
    activity = await sample_many(candidates, seconds=4.0)
    ranked = sorted(
        candidates,
        key=lambda symbol: (
            activity[symbol].activity_rate,
            activity[symbol].change_rate,
            activity[symbol].book_change_rate,
        ),
        reverse=True,
    )
    selected = ranked[0]
    stat = activity[selected]
    console.print(
        f"[bold green]DEMO TEST PAIR[/bold green] {selected} "
        f"activity={stat.activity_rate:.2f}/s trade_changes={stat.change_rate:.2f}/s "
        f"book_changes={stat.book_change_rate:.2f}/s"
    )
    return selected


class DemoMirror:
    def __init__(self, adapter: MexcWebExecutionAdapter, args: argparse.Namespace) -> None:
        self.adapter = adapter
        self.args = args
        self.positions: dict[str, MirroredPosition] = {}
        self.demo_pnl_usdt = 0.0
        self.zero_fee_pnl_usdt = 0.0
        self.mirrored_entries = 0
        self.mirrored_exits = 0
        self.skipped = 0

    async def _leverage(self, symbol: str) -> int:
        detail = await self.adapter.get_contract_detail(symbol)
        max_leverage = max(1, int(detail.get("maxLeverage") or 1))
        if int(self.args.demo_leverage) <= 0:
            return max_leverage
        return max(1, min(int(self.args.demo_leverage), max_leverage))

    async def entry(self, symbol: str, side_text: str, paper_filled_notional: float) -> None:
        if symbol in self.positions:
            console.print(f"[red]DEMO MIRROR REFUSE STACK[/red] {symbol}: mirror position already tracked")
            self.skipped += 1
            return

        side = OrderSide.LONG if side_text == "LONG" else OrderSide.SHORT
        try:
            existing = await self.adapter.get_position(symbol)
            if existing is not None:
                console.print(
                    f"[red]DEMO MIRROR REFUSE STACK[/red] {symbol}: remote position already open "
                    f"side={existing.side.value} qty={existing.qty:g}"
                )
                self.skipped += 1
                return

            leverage = await self._leverage(symbol)
            best = await self.adapter.get_best_price(symbol, side)
            if best <= 0:
                raise MexcWebError(f"invalid Demo best price for {symbol}")

            notional = max(0.0, float(paper_filled_notional))
            if self.args.demo_max_notional_usdt > 0:
                notional = min(notional, float(self.args.demo_max_notional_usdt))
            if notional <= 0:
                self.skipped += 1
                return

            requested_qty = notional / best
            cross = float(self.args.demo_ioc_cross_bps) / 10_000.0
            limit_price = best * (1.0 + cross if side is OrderSide.LONG else 1.0 - cross)
            timing: dict[str, float] = {}
            started_ms = time.time_ns() / 1_000_000.0
            fill = await self.adapter.open_ioc(
                symbol=symbol,
                side=side,
                price=limit_price,
                qty=requested_qty,
                leverage=leverage,
                client_order_id=f"baseline-v1-demo-{time.time_ns()}",
                timing_marks=timing,
            )
            ended_ms = time.time_ns() / 1_000_000.0

            remote = await demo._reconcile_ioc_position(self.adapter, symbol, side, fill)
            post_ms = timing.get("ioc_post_response_ms", ended_ms) - timing.get("ioc_post_start_ms", started_ms)
            confirm_ms = timing.get("ioc_confirmed_ms", ended_ms) - timing.get("ioc_post_start_ms", started_ms)

            if remote is None:
                console.print(
                    f"[yellow]DEMO NO FILL[/yellow] {symbol} {side_text} "
                    f"paper_filled=${paper_filled_notional:.0f} demo_requested=${notional:.0f} "
                    f"post={post_ms:.1f}ms confirm={confirm_ms:.1f}ms"
                )
                self.skipped += 1
                return

            entry_price = remote.entry_price or fill.avg_price or best
            self.positions[symbol] = MirroredPosition(
                symbol=symbol, side=side, entry_price=entry_price,
                entry_fee_usdt=float(fill.fee_usdt), leverage=leverage,
                qty=remote.qty, entry_time=time.monotonic(),
            )
            self.mirrored_entries += 1
            fill_ratio = remote.qty / requested_qty if requested_qty > 0 else 0.0
            console.print(
                f"[bold green]DEMO ENTRY[/bold green] {symbol} {side_text} "
                f"paper_filled=${paper_filled_notional:.0f} demo_requested=${notional:.0f} "
                f"actual_qty={remote.qty:g} fill_ratio={fill_ratio:.1%} entry={entry_price:g} "
                f"lev={leverage}x entry_fee=${fill.fee_usdt:.6f} "
                f"post={post_ms:.1f}ms confirm={confirm_ms:.1f}ms"
            )
        except MexcWebError as exc:
            console.print(f"[yellow]DEMO ENTRY SKIP[/yellow] {symbol}: {exc}")
            self.skipped += 1

    async def exit(self, symbol: str, reason: str) -> None:
        tracked = self.positions.get(symbol)
        if tracked is None:
            return
        try:
            remote = await self.adapter.get_position(symbol)
            if remote is None:
                console.print(f"[yellow]DEMO POSITION MISSING[/yellow] {symbol} at strategy EXIT {reason}")
                self.positions.pop(symbol, None)
                self.skipped += 1
                return

            fill = await demo._flatten_position(self.adapter, remote, f"baseline_v1_{reason}")
            total_fees = tracked.entry_fee_usdt + float(fill.fee_usdt)
            demo_pnl, price_pct, demo_roe = _trade_pnl(
                tracked.side, tracked.entry_price, fill.avg_price, fill.filled_qty,
                tracked.leverage, total_fees,
            )
            zero_fee_pnl, _, zero_fee_roe = _trade_pnl(
                tracked.side, tracked.entry_price, fill.avg_price, fill.filled_qty,
                tracked.leverage, 0.0,
            )
            self.demo_pnl_usdt += demo_pnl
            self.zero_fee_pnl_usdt += zero_fee_pnl
            self.mirrored_exits += 1
            duration = time.monotonic() - tracked.entry_time
            console.print(
                f"[bold cyan]DEMO EXIT[/bold cyan] {symbol} reason={reason} "
                f"exit={fill.avg_price:g} exit_fee=${fill.fee_usdt:.6f} total_fees=${total_fees:.6f}"
            )
            console.print(
                f"[bold]DEMO RESULT[/bold] net_after_both_fees={demo_pnl:+.6f}USDT "
                f"zero_fee_counterfactual={zero_fee_pnl:+.6f}USDT price={price_pct:+.4f}% "
                f"demo_ROE={demo_roe:+.2f}% zero_fee_ROE={zero_fee_roe:+.2f}% duration={duration:.3f}s "
                f"session_net={self.demo_pnl_usdt:+.6f}USDT"
            )
            self.positions.pop(symbol, None)
        except MexcWebError as exc:
            console.print(f"[red]DEMO EXIT ERROR[/red] {symbol}: {exc}")

    async def flatten_all(self) -> None:
        for symbol in list(self.positions):
            tracked = self.positions.get(symbol)
            if tracked is None:
                continue
            try:
                remote = await self.adapter.get_position(symbol)
                if remote is not None:
                    fill = await demo._flatten_position(self.adapter, remote, "mirror_cleanup")
                    total_fees = tracked.entry_fee_usdt + float(fill.fee_usdt)
                    pnl, _, _ = _trade_pnl(
                        tracked.side, tracked.entry_price, fill.avg_price, fill.filled_qty,
                        tracked.leverage, total_fees,
                    )
                    self.demo_pnl_usdt += pnl
                    console.print(f"[yellow]DEMO CLEANUP[/yellow] {symbol} pnl_after_fees={pnl:+.6f}USDT")
            except Exception as exc:
                console.print(f"[red]DEMO CLEANUP FAILED[/red] {symbol}: {type(exc).__name__}: {exc}")
            finally:
                self.positions.pop(symbol, None)


async def run(args: argparse.Namespace) -> int:
    demo._load_project_env()
    cfg = WebExecutionConfig.demo_from_env(write_enabled=True)
    _assert_demo_safety(cfg)

    console.print("[bold cyan]FROZEN BASELINE V1 / REAL MEXC DEMO EXECUTION TEST[/bold cyan]")
    console.print(
        "This is NOT production pair selection: Demo fee=0/0 and historical persistent-profile are not required."
    )
    console.print(
        "The chosen Testnet pair still uses the same BASELINE_V1 signal, measured RTT, retention, IOC/slippage/cost and exit thresholds."
    )
    console.print(
        "Real Demo IOC/reduce-only orders are sent through the Demo web token. BOTH opening and closing commissions are deducted."
    )

    async with MexcWebExecutionAdapter(cfg) as adapter:
        symbol = await _select_demo_test_symbol(adapter, args.demo_symbol)
        command = [
            sys.executable, "-u", "-m", "mexc_tick_scalper.demo_baseline_v1_signal_test",
            "--demo-test-symbol", symbol,
            "--target-closed-trades", str(int(args.target_closed_trades)),
            "--session-seconds", str(int(args.session_seconds)),
            "--max-signals", str(int(args.max_signals)),
        ]
        if args.lifetime_csv:
            command += ["--lifetime-csv", args.lifetime_csv]

        mirror = DemoMirror(adapter, args)
        proc = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=os.environ.copy(),
        )
        assert proc.stdout is not None
        try:
            while True:
                raw = await proc.stdout.readline()
                if not raw:
                    break
                line = raw.decode(errors="replace").rstrip()
                console.print(line, markup=False)

                entry = ENTRY_RE.search(line)
                if entry:
                    await mirror.entry(entry.group("symbol"), entry.group("side"), float(entry.group("filled")))
                    continue
                exit_match = EXIT_RE.search(line)
                if exit_match:
                    await mirror.exit(exit_match.group("symbol"), exit_match.group("reason"))

            code = await proc.wait()
        finally:
            if proc.returncode is None:
                proc.terminate()
                try:
                    await asyncio.wait_for(proc.wait(), timeout=3.0)
                except TimeoutError:
                    proc.kill(); await proc.wait()
            await mirror.flatten_all()

        console.print(
            f"[bold cyan]DEMO SUMMARY[/bold cyan] symbol={symbol} entries={mirror.mirrored_entries} "
            f"exits={mirror.mirrored_exits} skipped={mirror.skipped} "
            f"NET_AFTER_DEMO_FEES={mirror.demo_pnl_usdt:+.6f}USDT "
            f"ZERO_FEE_COUNTERFACTUAL={mirror.zero_fee_pnl_usdt:+.6f}USDT child_exit={code}"
        )
        return code


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Run frozen BASELINE_V1 mechanics on an active MEXC Testnet pair and execute real Demo orders")
    p.add_argument("--target-closed-trades", type=int, default=100)
    p.add_argument("--session-seconds", type=int, default=86400)
    p.add_argument("--max-signals", type=int, default=3000)
    p.add_argument("--lifetime-csv", default="")
    p.add_argument("--demo-symbol", default="", help="Optional explicit Testnet symbol; blank = automatic active cross-listed pair")
    p.add_argument("--demo-leverage", type=int, default=0, help="0 = Testnet contract maximum; execution-only setting")
    p.add_argument("--demo-max-notional-usdt", type=float, default=0.0, help="0 = mirror paper-filled notional; positive value caps only Demo size")
    p.add_argument("--demo-ioc-cross-bps", type=float, default=1.0)
    return p


def main() -> None:
    args = build_parser().parse_args()
    if args.target_closed_trades <= 0 or args.session_seconds <= 0 or args.max_signals <= 0:
        raise SystemExit("trade/session/signal limits must be positive")
    if args.demo_max_notional_usdt < 0:
        raise SystemExit("--demo-max-notional-usdt must be >= 0")
    if args.demo_ioc_cross_bps <= 0:
        raise SystemExit("--demo-ioc-cross-bps must be > 0")
    try:
        raise SystemExit(asyncio.run(run(args)))
    except MexcWebError as exc:
        console.print(f"[red]DEMO BASELINE TEST FAILED:[/red] {exc}")
        raise SystemExit(2) from exc


if __name__ == "__main__":
    main()
