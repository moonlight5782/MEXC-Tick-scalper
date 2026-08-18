from __future__ import annotations

import os
from pathlib import Path


def project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def load_project_env(path: Path | None = None) -> Path:
    """Load simple KEY=VALUE entries from project .env without third-party dependencies.

    Existing process environment always wins. This intentionally supports only the
    ordinary .env syntax used by this repository; secrets are never printed.
    """
    env_path = path or (project_root() / ".env")
    if not env_path.exists():
        return env_path
    for raw in env_path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        os.environ.setdefault(key, value)
    return env_path


def require_demo_write_enabled() -> None:
    if os.getenv("MEXC_DEMO_WRITE", "").strip().upper() != "YES":
        raise RuntimeError("MEXC_DEMO_WRITE must be explicitly YES for Testnet order writes")
