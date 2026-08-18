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


def _patch_profile(monkeypatch, tmp_path, *, production_symbol="KEEP_USDT", testnet_rows=None):
    lifetime = tmp_path / "lifetime.csv"
    lifetime.write_text("placeholder", encoding="utf-8")
    runner._LIFETIME_CSV = str(lifetime)
    monkeypatch.setattr(runner, "build_profiles", lambda source: ["profiles"])
    monkeypatch.setattr(
        runner,
        "select_profiles",
        lambda *a, **k: [SimpleNamespace(symbol=production_symbol)],
    )
    monkeypatch.setattr(
        runner,
        "discover_live_zero_fee_crosslisted",
        lambda: _async_result([SimpleNamespace(mexc_symbol=production_symbol)]),
    )
    monkeypatch.setattr(
        runner,
        "_testnet_contract_rows",
        lambda adapter: _async_result(testnet_rows or {}),
    )


def test_testnet_is_only_final_intersection(monkeypatch, tmp_path):
    _patch_profile(
        monkeypatch,
        tmp_path,
        testnet_rows={"KEEP_USDT": {"symbol": "KEEP_USDT"}},
    )
    selected, details = asyncio.run(runner._frozen_execution_universe(FakeAdapter()))
    assert [x.mexc_symbol for x in selected] == ["KEEP_USDT"]
    assert set(details) == {"KEEP_USDT"}
    assert runner._FIDELITY_MODE == "FULL_FIDELITY"


def test_no_overlap_falls_back_only_for_execution_mechanics(monkeypatch, tmp_path):
    _patch_profile(monkeypatch, tmp_path, production_symbol="PROD_USDT", testnet_rows={})
    fallback_contract = SimpleNamespace(mexc_symbol="BTC_USDT")
    monkeypatch.setattr(
        runner,
        "_execution_only_testnet_universe",
        lambda adapter: _async_result(([fallback_contract], {"BTC_USDT": {"symbol": "BTC_USDT"}})),
    )
    selected, details = asyncio.run(runner._frozen_execution_universe(FakeAdapter()))
    assert [x.mexc_symbol for x in selected] == ["BTC_USDT"]
    assert set(details) == {"BTC_USDT"}
    assert runner._FIDELITY_MODE == "TESTNET_EXECUTION_ONLY"


def test_summary_always_labels_fidelity_mode():
    runner._FIDELITY_MODE = "TESTNET_EXECUTION_ONLY"
    wrapped = runner._mode_summary(lambda stats, target: "closed=1/100")
    assert wrapped(None, 100) == "MODE=TESTNET_EXECUTION_ONLY closed=1/100"


async def _async_value(value):
    return value


def _async_result(value):
    return _async_value(value)
