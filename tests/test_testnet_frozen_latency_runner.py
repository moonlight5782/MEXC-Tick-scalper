import asyncio
from types import SimpleNamespace

from mexc_tick_scalper.baseline_v1 import BASELINE_V1, apply_baseline_v1
from mexc_tick_scalper import testnet_frozen_latency_runner as runner


class FakeAdapter:
    async def get_best_price(self, symbol, side):
        return 101.0 if side.value == "long" else 100.0


def test_frozen_baseline_values_remain_unchanged():
    args = SimpleNamespace(
        target_notional_usdt=1.0,
        min_absolute_residual_bps=1.0,
        min_signal_strength_ratio=1.0,
        ioc_cross_bps=9.0,
    )
    apply_baseline_v1(args)
    assert args.target_notional_usdt == BASELINE_V1["target_notional_usdt"] == 10000.0
    assert args.min_absolute_residual_bps == BASELINE_V1["min_absolute_residual_bps"] == 8.0
    assert args.min_signal_strength_ratio == BASELINE_V1["min_signal_strength_ratio"] == 3.0
    assert args.ioc_cross_bps == BASELINE_V1["ioc_cross_bps"] == 1.0


def test_testnet_is_only_final_intersection(monkeypatch, tmp_path):
    lifetime = tmp_path / "lifetime.csv"
    lifetime.write_text("placeholder", encoding="utf-8")
    runner._LIFETIME_CSV = str(lifetime)

    monkeypatch.setattr(runner, "build_profiles", lambda source: ["profiles"])
    monkeypatch.setattr(
        runner,
        "select_profiles",
        lambda *a, **k: [SimpleNamespace(symbol="KEEP_USDT")],
    )
    monkeypatch.setattr(
        runner,
        "discover_live_zero_fee_crosslisted",
        lambda: _async_result([
            SimpleNamespace(mexc_symbol="KEEP_USDT"),
            SimpleNamespace(mexc_symbol="NOT_PERSISTENT_USDT"),
        ]),
    )
    monkeypatch.setattr(
        runner,
        "_testnet_contract_rows",
        lambda adapter: _async_result({"KEEP_USDT": {"symbol": "KEEP_USDT"}}),
    )

    selected, details = asyncio.run(runner._frozen_execution_universe(FakeAdapter()))
    assert [x.mexc_symbol for x in selected] == ["KEEP_USDT"]
    assert set(details) == {"KEEP_USDT"}


async def _async_value(value):
    return value


def _async_result(value):
    return _async_value(value)
