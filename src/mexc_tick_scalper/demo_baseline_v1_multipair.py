from __future__ import annotations

import argparse
import asyncio
import os
import sys

from rich.console import Console

from .demo_baseline_v1_mirror import DemoMirror, ENTRY_RE, EXIT_RE, _usable_demo_symbols
from .demo_smoke import _assert_demo_safety
from .lead_lag import fetch_binance_usdm_symbols, mexc_to_binance_symbol
from .market import MexcPublicMarket
from .web_execution import MexcWebError, MexcWebExecutionAdapter, WebExecutionConfig

console = Console()
LIVE_REST = "https://contract.mexc.com"
LIVE_WS = "wss://contract.mexc.com/edge"


def _parse_symbols(raw: str) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in (raw or "").split(","):
        symbol = item.strip().upper()
        if symbol and symbol not in seen:
            seen.add(symbol)
            out.append(symbol)
    return out


async def _select_test_universe(adapter: MexcWebExecutionAdapter, requested_raw: str) -> list[str]:
    """Exact same-symbol universe for signal + real Testnet execution.

    A symbol must exist on MEXC Testnet, LIVE MEXC and Binance USD-M. Testnet
    activity is not a selection signal: lead/lag is measured on LIVE market data.
    """
    demo_symbols = set(await _usable_demo_symbols(adapter))
    if not demo_symbols:
        raise MexcWebError("Testnet has no usable contracts with a two-sided book")

    binance_symbols = await fetch_binance_usdm_symbols()
    live_rows = await MexcPublicMarket(LIVE_REST, LIVE_WS).contracts()
    live_symbols = {str(row.get("symbol") or "").upper() for row in live_rows}

    candidates = sorted(
        symbol
        for symbol in demo_symbols
        if symbol in live_symbols and mexc_to_binance_symbol(symbol) in binance_symbols
    )
    if not candidates:
        raise MexcWebError("No exact symbol exists on MEXC Testnet + LIVE MEXC + Binance USD-M")

    requested = _parse_symbols(requested_raw)
    if requested:
        missing = [symbol for symbol in requested if symbol not in candidates]
        if missing:
            raise MexcWebError("Requested symbols are not exact Testnet+LIVE+Binance matches: " + ",".join(missing))
        return requested

    return candidates


async def run(args: argparse.Namespace) -> int:
    from . import demo_hybrid_test as demo

    demo._load_project_env()
    cfg = WebExecutionConfig.demo_from_env(write_enabled=True)
    _assert_demo_safety(cfg)

    console.print("[bold cyan]FROZEN BASELINE V1 / SAME-SYMBOL REAL MEXC DEMO TEST[/bold cyan]")
    console.print(
        "Automatically monitors every exact symbol present on Binance USD-M, LIVE MEXC and MEXC Testnet."
    )
    console.print(
        "The strongest qualifying LIVE Binance->MEXC signal is selected and the REAL Testnet IOC is sent on THAT SAME symbol."
    )
    console.print(
        "No proxy substitution and no Testnet activity ranking. BASELINE_V1 signal/RTT/retention/IOC/slippage/cost/exits remain unchanged. BOTH Demo entry and exit fees are deducted."
    )

    async with MexcWebExecutionAdapter(cfg) as adapter:
        symbols = await _select_test_universe(adapter, args.demo_symbols)
        console.print(
            f"[bold green]SAME-SYMBOL DEMO UNIVERSE[/bold green] {len(symbols)} pair(s): " + ",".join(symbols)
        )

        command = [
            sys.executable,
            "-u",
            "-m",
            "mexc_tick_scalper.demo_baseline_v1_signal_test",
            "--demo-test-symbols",
            ",".join(symbols),
            "--target-closed-trades",
            str(int(args.target_closed_trades)),
            "--session-seconds",
            str(int(args.session_seconds)),
            "--max-signals",
            str(int(args.max_signals)),
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
                    proc.kill()
                    await proc.wait()
            await mirror.flatten_all()

        console.print(
            f"[bold cyan]DEMO SAME-SYMBOL SUMMARY[/bold cyan] universe={len(symbols)} "
            f"entries={mirror.mirrored_entries} exits={mirror.mirrored_exits} skipped={mirror.skipped} "
            f"NET_AFTER_DEMO_FEES={mirror.demo_pnl_usdt:+.6f}USDT "
            f"ZERO_FEE_COUNTERFACTUAL={mirror.zero_fee_pnl_usdt:+.6f}USDT child_exit={code}"
        )
        return code


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Automatically execute on Testnet the same symbol that produced a frozen BASELINE_V1 Binance->MEXC signal"
    )
    p.add_argument("--target-closed-trades", type=int, default=100)
    p.add_argument("--session-seconds", type=int, default=86400)
    p.add_argument("--max-signals", type=int, default=3000)
    p.add_argument("--lifetime-csv", default="")
    p.add_argument("--demo-symbols", default="", help="Optional exact-symbol subset; blank = auto-discover all matches")
    p.add_argument("--demo-leverage", type=int, default=0, help="0 = Testnet contract maximum")
    p.add_argument("--demo-max-notional-usdt", type=float, default=0.0, help="0 = mirror paper-filled notional")
    p.add_argument("--demo-ioc-cross-bps", type=float, default=1.0)
    return p


def main() -> None:
    args = build_parser().parse_args()
    if args.target_closed_trades <= 0 or args.session_seconds <= 0 or args.max_signals <= 0:
        raise SystemExit("trade/session/signal limits must be positive")
    if args.demo_max_notional_usdt < 0 or args.demo_ioc_cross_bps <= 0:
        raise SystemExit("invalid Demo execution sizing/cross")
    try:
        raise SystemExit(asyncio.run(run(args)))
    except MexcWebError as exc:
        console.print(f"[red]DEMO SAME-SYMBOL TEST FAILED:[/red] {exc}")
        raise SystemExit(2) from exc


if __name__ == "__main__":
    main()
