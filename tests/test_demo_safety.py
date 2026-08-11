import pytest

from mexc_tick_scalper.demo_smoke import _assert_demo_safety
from mexc_tick_scalper.web_execution import DEMO_HOST, MexcWebError, WebExecutionConfig


def _demo_config(*, base_url: str, origin: str | None = None, referer: str | None = None) -> WebExecutionConfig:
    demo_origin = origin or f"https://{DEMO_HOST}"
    demo_referer = referer or f"https://{DEMO_HOST}/futures/BTC_USDT"
    return WebExecutionConfig(
        auth_token="WEB_TEST_TOKEN",
        base_url=base_url,
        origin=demo_origin,
        referer=demo_referer,
        write_enabled=True,
        environment="demo",
    )


def test_live_url_is_always_rejected_for_demo_smoke(monkeypatch):
    monkeypatch.setenv("MEXC_DEMO_WRITE", "YES")
    cfg = _demo_config(base_url="https://futures.mexc.com/api/v1")
    with pytest.raises(MexcWebError, match="refuses non-testnet host"):
        _assert_demo_safety(cfg)


def test_demo_requires_explicit_environment_confirmation(monkeypatch):
    monkeypatch.delenv("MEXC_DEMO_WRITE", raising=False)
    cfg = _demo_config(base_url=f"https://{DEMO_HOST}/api/v1")
    with pytest.raises(MexcWebError, match="MEXC_DEMO_WRITE=YES"):
        _assert_demo_safety(cfg)


def test_demo_guard_accepts_testnet_with_confirmation(monkeypatch):
    monkeypatch.setenv("MEXC_DEMO_WRITE", "YES")
    cfg = _demo_config(base_url=f"https://{DEMO_HOST}/api/v1")
    _assert_demo_safety(cfg)
