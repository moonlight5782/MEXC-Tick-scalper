import os
from pathlib import Path

import pytest

from mexc_tick_scalper.canonical_env import load_project_env, require_demo_write_enabled


def test_load_project_env_preserves_existing_process_values(tmp_path: Path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("A=from_file\nB='quoted value'\n# comment\n", encoding="utf-8")
    monkeypatch.setenv("A", "from_process")
    monkeypatch.delenv("B", raising=False)
    load_project_env(env_file)
    assert os.environ["A"] == "from_process"
    assert os.environ["B"] == "quoted value"


def test_demo_write_gate_requires_explicit_yes(monkeypatch):
    monkeypatch.setenv("MEXC_DEMO_WRITE", "NO")
    with pytest.raises(RuntimeError):
        require_demo_write_enabled()
    monkeypatch.setenv("MEXC_DEMO_WRITE", "YES")
    require_demo_write_enabled()
