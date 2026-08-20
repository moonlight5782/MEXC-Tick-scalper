import pytest

import mexc_tick_scalper.auto_discovery_testnet as tn
from mexc_tick_scalper.execution import OrderSide
from mexc_tick_scalper.web_execution import DEMO_HOST, MexcWebError, WebExecutionConfig


def _demo_cfg(*, write_enabled: bool = True) -> WebExecutionConfig:
    return WebExecutionConfig(
        auth_token="WEB-test-token",
        base_url=f"https://{DEMO_HOST}/api/v1",
        origin=f"https://{DEMO_HOST}",
        referer=f"https://{DEMO_HOST}/futures/BTC_USDT",
        write_enabled=write_enabled,
        environment="demo",
    )


def test_testnet_runner_requires_explicit_demo_write(monkeypatch) -> None:
    monkeypatch.delenv("MEXC_DEMO_WRITE", raising=False)
    with pytest.raises(MexcWebError):
        tn._assert_demo_write_config(_demo_cfg())

    monkeypatch.setenv("MEXC_DEMO_WRITE", "YES")
    tn._assert_demo_write_config(_demo_cfg())


def test_testnet_runner_refuses_live_host_even_with_demo_write(monkeypatch) -> None:
    monkeypatch.setenv("MEXC_DEMO_WRITE", "YES")
    cfg = WebExecutionConfig(
        auth_token="WEB-test-token",
        base_url="https://futures.mexc.com/api/v1",
        origin=f"https://{DEMO_HOST}",
        referer=f"https://{DEMO_HOST}/futures/BTC_USDT",
        write_enabled=True,
        environment="demo",
    )
    with pytest.raises(MexcWebError):
        tn._assert_demo_write_config(cfg)


def test_same_dynamic_risk_sizing_is_preserved() -> None:
    bank = tn.Bank(balance_usdt=100.0)

    requested, margin, reserve = tn._requested_notional(bank, 200.0)
    assert requested == pytest.approx(10_000.0)
    assert margin == pytest.approx(50.0)
    assert reserve == pytest.approx(50.0)

    requested, margin, reserve = tn._requested_notional(bank, 50.0)
    assert requested == pytest.approx(4_000.0)
    assert margin == pytest.approx(80.0)
    assert reserve == pytest.approx(20.0)


def test_effective_leverage_is_capped_by_live_and_testnet_contracts() -> None:
    assert tn._effective_leverage(125, {"maxLeverage": 100}) == 100
    assert tn._effective_leverage(250, {"maxLeverage": 250}) == 200
    assert tn._effective_leverage(20, {"maxLeverage": 125}) == 20


def test_profit_runner_disables_only_convergence() -> None:
    assert tn._convergence_exit_allowed(False)
    assert not tn._convergence_exit_allowed(True)


def test_demo_ioc_price_crosses_one_bps_and_respects_tick() -> None:
    long_price = tn._demo_ioc_price(100.0, OrderSide.LONG, 1.0, 0.01)
    short_price = tn._demo_ioc_price(100.0, OrderSide.SHORT, 1.0, 0.01)
    assert long_price == pytest.approx(100.01)
    assert short_price == pytest.approx(99.99)
