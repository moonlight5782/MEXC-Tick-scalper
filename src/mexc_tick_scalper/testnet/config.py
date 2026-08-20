from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

from ..web_execution import DEMO_HOST, WebExecutionConfig


@dataclass(frozen=True, slots=True)
class TestnetBootstrap:
    env_path: Path
    readonly_execution: WebExecutionConfig
    trading_execution: WebExecutionConfig


def _validate_demo_write(config: WebExecutionConfig) -> None:
    config.validate_environment()
    if config.environment != "demo":
        raise RuntimeError(f"Testnet write config has unexpected environment {config.environment!r}")
    if not config.write_enabled:
        raise RuntimeError("Testnet trading config must have writes explicitly enabled")
    if os.getenv("MEXC_DEMO_WRITE", "").strip().upper() != "YES":
        raise RuntimeError("Demo writes are locked. Set MEXC_DEMO_WRITE=YES in local .env.")
    if DEMO_HOST not in config.base_url:
        raise RuntimeError("Testnet trading config refuses a non-Demo execution host")


def load_testnet_bootstrap() -> TestnetBootstrap:
    """Load project .env once and build explicit Demo read/write dependencies."""
    env_path = Path(__file__).resolve().parents[3] / ".env"
    load_dotenv(env_path, override=False)
    try:
        readonly = WebExecutionConfig.demo_from_env(write_enabled=False)
        trading = WebExecutionConfig.demo_from_env(write_enabled=True)
        _validate_demo_write(trading)
    except Exception as exc:
        raise RuntimeError(
            f"Invalid Testnet configuration after loading {env_path}: {exc}"
        ) from exc
    return TestnetBootstrap(
        env_path=env_path,
        readonly_execution=readonly,
        trading_execution=trading,
    )
