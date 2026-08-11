import pytest

from mexc_tick_scalper.web_execution import DEMO_HOST, MexcWebError, WebExecutionConfig


def test_demo_config_accepts_only_testnet_host():
    cfg = WebExecutionConfig(
        auth_token="WEB-test-token",
        base_url=f"https://{DEMO_HOST}/api/v1",
        origin=f"https://{DEMO_HOST}",
        referer=f"https://{DEMO_HOST}/futures/BTC_USDT",
        environment="demo",
    )
    cfg.validate_environment()


def test_demo_config_rejects_live_futures_host():
    cfg = WebExecutionConfig(
        auth_token="WEB-test-token",
        base_url="https://futures.mexc.com/api/v1",
        origin=f"https://{DEMO_HOST}",
        referer=f"https://{DEMO_HOST}/futures/BTC_USDT",
        environment="demo",
    )
    with pytest.raises(MexcWebError):
        cfg.validate_environment()


def test_demo_config_rejects_live_origin_or_referer():
    cfg = WebExecutionConfig(
        auth_token="WEB-test-token",
        base_url=f"https://{DEMO_HOST}/api/v1",
        origin="https://www.mexc.com",
        referer=f"https://{DEMO_HOST}/futures/BTC_USDT",
        environment="demo",
    )
    with pytest.raises(MexcWebError):
        cfg.validate_environment()
