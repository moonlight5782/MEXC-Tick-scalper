from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

from ..web_execution import WebExecutionConfig


@dataclass(frozen=True, slots=True)
class TestnetBootstrap:
    env_path: Path
    readonly_execution: WebExecutionConfig


def load_testnet_bootstrap() -> TestnetBootstrap:
    """Load project .env once and construct the read-only Demo dependency.

    Trading write validation remains inside the execution compatibility bridge until
    that legacy engine is replaced. Discovery/configuration never constructs LIVE
    private auth.
    """
    env_path = Path(__file__).resolve().parents[3] / ".env"
    load_dotenv(env_path, override=False)
    try:
        readonly = WebExecutionConfig.demo_from_env(write_enabled=False)
    except Exception as exc:
        raise RuntimeError(
            f"Invalid Testnet configuration after loading {env_path}: {exc}"
        ) from exc
    return TestnetBootstrap(env_path=env_path, readonly_execution=readonly)
