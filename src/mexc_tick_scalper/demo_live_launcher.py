from __future__ import annotations

import asyncio
import math
import os
import subprocess
import sys
from pathlib import Path

from dotenv import load_dotenv
from rich.console import Console

from .demo_position_manager import flatten_all_demo_positions
from .web_execution import MexcWebError

console = Console()


def _load_env() -> None:
    root = Path(__file__).resolve().parents[2]
    load_dotenv(root / ".env", override=False)


def _candidate_score(events: int, avg_edge_bps: float, live_spread_bps: float) -> float:
    """Compatibility helper retained for scanner/tests."""
    net = float(avg_edge_bps) - float(live_spread_bps)
    return net * math.sqrt(max(1, int(events)))


def _ask_int(prompt: str, default: int) -> int:
    raw = input(f"{prompt} [{default}]: ").strip()
    return default if not raw else int(raw)


def _ask_float(prompt: str, default: float) -> float:
    raw = input(f"{prompt} [{default:g}]: ").strip()
    return default if not raw else float(raw)


def _cleanup_sync(reason: str) -> bool:
    try:
        asyncio.run(flatten_all_demo_positions(reason=reason))
        return True
    except Exception as exc:
        console.print(f"[red]DEMO CLEANUP FAILED[/red] ({reason}): {exc}")
        return False


def main() -> None:
    _load_env()
    child: subprocess.Popen | None = None
    exit_code = 0

    try:
        asyncio.run(flatten_all_demo_positions(reason="startup"))
        console.print(
            "[cyan]LIVE MICROSPREAD / DEMO EXECUTION[/cyan]\n"
            "Binance and MEXC order books are monitored continuously. The bot trades short-lived deviations from the "
            "normal Binance/MEXC basis, while every order/position write remains on MEXC TESTNET only."
        )

        leverage = _ask_int("Leverage cap", 50)
        cycles = _ask_int("Max cycles", 50)
        seconds = _ask_int("Max session seconds", 1800)
        margin = _ask_float("Demo isolated margin cap per IOC cycle, USDT", 0.10)
        if margin <= 0:
            raise ValueError("Demo margin cap must be positive; max-balance sizing is not available from start_demo.bat")

        cmd = [
            sys.executable,
            "-m",
            "mexc_tick_scalper.demo_microspread_test",
            "--session-seconds",
            str(seconds),
            "--max-cycles",
            str(cycles),
            "--leverage",
            str(leverage),
        ]
        cmd += ["--target-margin-usdt", str(margin)]
        child = subprocess.Popen(cmd, env=os.environ.copy())
        exit_code = child.wait()
    except KeyboardInterrupt:
        console.print("\n[yellow]STOP REQUESTED[/yellow]: stopping strategy and flattening Demo account...")
        exit_code = 130
        if child is not None and child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=2)
    except (MexcWebError, ValueError) as exc:
        console.print(f"[red]LIVE DEMO LAUNCHER FAILED:[/red] {exc}")
        exit_code = 2
    except Exception as exc:
        console.print(f"[red]UNEXPECTED LIVE DEMO ERROR:[/red] {type(exc).__name__}: {exc}")
        exit_code = 3
    finally:
        cleanup_ok = _cleanup_sync("shutdown")
        if not cleanup_ok and exit_code == 0:
            exit_code = 4

    raise SystemExit(exit_code)


if __name__ == "__main__":
    main()
