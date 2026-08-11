import pytest

from mexc_tick_scalper.demo_execution import (
    DEMO_HOST,
    DemoSessionConfig,
    mexc_close_side,
    mexc_open_side,
)
from mexc_tick_scalper.execution import OrderSide


def test_demo_adapter_rejects_live_host():
    cfg = DemoSessionConfig(
        base_url="https://www.mexc.com",
        cookie="demo-cookie",
    )
    with pytest.raises(ValueError):
        cfg.validate()


def test_demo_adapter_accepts_testnet_only():
    cfg = DemoSessionConfig(
        base_url=f"https://{DEMO_HOST}",
        cookie="demo-cookie",
    )
    cfg.validate()


def test_demo_adapter_requires_cookie():
    cfg = DemoSessionConfig(base_url=f"https://{DEMO_HOST}", cookie="")
    with pytest.raises(ValueError):
        cfg.validate()


def test_mexc_side_mapping():
    assert mexc_open_side(OrderSide.LONG) == 1
    assert mexc_open_side(OrderSide.SHORT) == 3
    # action SHORT closes an existing LONG; action LONG closes an existing SHORT
    assert mexc_close_side(OrderSide.SHORT) == 4
    assert mexc_close_side(OrderSide.LONG) == 2
