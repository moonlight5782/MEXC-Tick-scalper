import asyncio

import mexc_tick_scalper.live_zero_fee_universe as live_universe
from mexc_tick_scalper.fees import SnapshotFeeProvider
from mexc_tick_scalper.models import FeeStatus


def test_live_zero_fee_universe_intersects_account_fees_and_binance(monkeypatch):
    async def fake_binance_symbols():
        return {"BTCUSDT", "ETHUSDT"}

    async def fake_contracts(self):
        return [
            {"symbol": "BTC_USDT", "maxLeverage": 200, "contractSize": 0.001, "minVol": 1},
            {"symbol": "ETH_USDT", "maxLeverage": 100, "contractSize": 0.01, "minVol": 1},
            {"symbol": "XRP_USDT", "maxLeverage": 50, "contractSize": 1, "minVol": 1},
        ]

    class FakeConfig:
        write_enabled = False

    class FakeAdapter:
        def __init__(self, cfg):
            assert cfg.write_enabled is False

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

    async def fake_fee_provider(adapter):
        return SnapshotFeeProvider(
            {
                "BTC_USDT": FeeStatus(0.0, 0.0, "test"),
                "ETH_USDT": FeeStatus(0.0, 0.0001, "test"),
                "XRP_USDT": FeeStatus(0.0, 0.0, "test"),
            }
        )

    monkeypatch.setattr(live_universe, "fetch_binance_usdm_symbols", fake_binance_symbols)
    monkeypatch.setattr(live_universe.MexcPublicMarket, "contracts", fake_contracts)
    monkeypatch.setattr(
        live_universe.WebExecutionConfig,
        "from_env",
        classmethod(lambda cls, **kwargs: FakeConfig()),
    )
    monkeypatch.setattr(live_universe, "MexcWebExecutionAdapter", FakeAdapter)
    monkeypatch.setattr(live_universe, "read_web_fee_provider", fake_fee_provider)

    rows = asyncio.run(live_universe.discover_live_zero_fee_crosslisted())

    assert [row.mexc_symbol for row in rows] == ["BTC_USDT"]
    assert rows[0].binance_symbol == "BTCUSDT"
