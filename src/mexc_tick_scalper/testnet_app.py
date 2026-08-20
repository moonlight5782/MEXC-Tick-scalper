from __future__ import annotations

import argparse
import asyncio

from rich.console import Console

from .baseline_v1 import apply_baseline_v1
from .testnet.config import load_testnet_bootstrap
from .testnet.scanner import LeadLagScanner
from .testnet.selector import PairSelector
from .testnet.session import TradingSession
from .testnet.universe import TestnetUniverseService


console = Console()


class TestnetApp:
    """Compose the Testnet product from independent blocks."""

    def __init__(self, args, execution_config, app_console=console) -> None:
        self.args = args
        self.console = app_console
        self.universe = TestnetUniverseService(app_console, execution_config)
        self.scanner = LeadLagScanner(args, app_console)
        self.selector = PairSelector(app_console)
        self.trading = TradingSession(args, app_console)

    async def run(self) -> None:
        self.console.print("[bold cyan]TESTNET APP[/bold cyan]")
        self.console.print(
            "Pipeline: configuration -> universe -> discovery -> selection -> trading. "
            "Testnet discovery never requires MEXC_WEB_TOKEN."
        )

        scope = self.selector.ask_fee_scope()
        universe = await self.universe.load(scope)
        if not universe:
            raise RuntimeError("No Testnet-compatible contracts in the selected fee scope")

        candidates = await self.scanner.scan(universe)
        if not candidates:
            raise RuntimeError(
                "Fresh scan saw no discovery-grade lead-lag event. Run again or increase --scan-seconds; "
                "real trading entry remains fixed at 8bps/3x."
            )

        self.selector.show(candidates)
        selected = self.selector.ask_pair(candidates)
        await self.trading.run(selected)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Structured Testnet app: universe -> live discovery -> pair selection -> trading"
    )
    parser.add_argument("--discovery-top", type=int, default=5)
    parser.add_argument(
        "--scan-seconds",
        type=float,
        default=30.0,
        help="fresh pre-trade observation window; only before trading starts",
    )
    parser.add_argument("--profit-runner-arm-bps", type=float, default=5.0)
    parser.add_argument("--target-closed-trades", type=int, default=100)
    parser.add_argument("--session-seconds", type=float, default=1800.0)
    parser.add_argument("--max-signals", type=int, default=300)
    parser.add_argument("--testnet-output", default="persistent_end2end_TESTNET.csv")
    parser.add_argument("--append-output", action="store_true")
    return parser


def _validate_operational_args(args) -> None:
    if args.discovery_top <= 0:
        raise RuntimeError("--discovery-top must be positive")
    if args.scan_seconds <= 0:
        raise RuntimeError("--scan-seconds must be positive")
    if args.profit_runner_arm_bps < 0:
        raise RuntimeError("--profit-runner-arm-bps must be non-negative")
    if args.target_closed_trades <= 0:
        raise RuntimeError("--target-closed-trades must be positive")
    if args.session_seconds <= 0:
        raise RuntimeError("--session-seconds must be positive")
    if args.max_signals <= 0:
        raise RuntimeError("--max-signals must be positive")


def main() -> None:
    try:
        bootstrap = load_testnet_bootstrap()
        console.print(f"[cyan]CONFIG[/cyan] Loaded Testnet environment from {bootstrap.env_path}")

        args = build_parser().parse_args()
        apply_baseline_v1(args)
        _validate_operational_args(args)

        asyncio.run(
            TestnetApp(
                args,
                execution_config=bootstrap.readonly_execution,
                app_console=console,
            ).run()
        )
    except KeyboardInterrupt:
        raise SystemExit(130)
    except Exception as exc:
        console.print(f"[red]TESTNET APP STOPPED:[/red] {type(exc).__name__}: {exc}")
        raise SystemExit(2)


if __name__ == "__main__":
    main()
