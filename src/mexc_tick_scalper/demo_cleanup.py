from __future__ import annotations

import asyncio
from pathlib import Path

from dotenv import load_dotenv
from rich.console import Console

from .demo_position_manager import flatten_all_demo_positions
from .web_execution import MexcWebError

console = Console()


def main() -> None:
    root = Path(__file__).resolve().parents[2]
    load_dotenv(root / ".env", override=False)
    try:
        asyncio.run(flatten_all_demo_positions(reason="manual-cleanup"))
    except MexcWebError as exc:
        console.print(f"[red]DEMO CLEANUP FAILED:[/red] {exc}")
        raise SystemExit(2) from exc
    console.print("[green]Demo account is flat.[/green]")


if __name__ == "__main__":
    main()
