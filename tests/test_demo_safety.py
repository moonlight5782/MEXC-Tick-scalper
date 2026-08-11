import os

import pytest

from mexc_tick_scalper.demo_smoke import _assert_demo_safety
from mexc_tick_scalper.web_execution import MexcWebError


def test_live_url_is_always_rejected_for_demo_smoke(monkeypatch):
    monkeypatch.setenv("MEXC_DEMO_WRITE", "YES")
    with pytest.raises(MexcWebError, match="does not look like Demo/Testnet"):
        _assert_demo_safety("https://futures.mexc.com/api/v1")


def test_demo_requires_explicit_environment_confirmation(monkeypatch):
    monkeypatch.delenv("MEXC_DEMO_WRITE", raising=False)
    with pytest.raises(MexcWebError, match="MEXC_DEMO_WRITE=YES"):
        _assert_demo_safety("https://futures.testnet.example/api/v1")


def test_demo_guard_accepts_testnet_with_confirmation(monkeypatch):
    monkeypatch.setenv("MEXC_DEMO_WRITE", "YES")
    _assert_demo_safety("https://futures.testnet.example/api/v1")
