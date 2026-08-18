import asyncio
import time
from types import SimpleNamespace

from mexc_tick_scalper.baseline_v1 import BASELINE_V1, apply_baseline_v1
from mexc_tick_scalper.execution import OrderSide
from mexc_tick_scalper.microspread_feed import LiveBook
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
    runner._TESTNET_FEED = None
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
    monkeypatch.setattr(runner, "_start_testnet_ws_cache", lambda symbols: _async_result(None))


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


def test_cached_testnet_quote_avoids_rest(monkeypatch):
    now_ms = int(time.time() * 1000)
    runner._TESTNET_FEED = SimpleNamespace(
        books={
            "BTC_USDT": LiveBook(
                bid=99.0,
                ask=101.0,
                recv_ms=now_ms,
                exchange_ts_ms=now_ms,
                bids=((99.0, 1.0),),
                asks=((101.0, 1.0),),
            )
        }
    )

    async def fail_rest(*args, **kwargs):
        raise AssertionError("REST depth must not be called for a fresh WS quote")

    monkeypatch.setattr(runner, "_ORIGINAL_GET_BEST_PRICE", fail_rest)
    long_price = asyncio.run(runner._cached_get_best_price(FakeAdapter(), "BTC_USDT", OrderSide.LONG))
    short_price = asyncio.run(runner._cached_get_best_price(FakeAdapter(), "BTC_USDT", OrderSide.SHORT))
    assert long_price == 101.0
    assert short_price == 99.0
    runner._TESTNET_FEED = None


def test_position_clock_is_rebased_to_ioc_submit(monkeypatch):
    captured = {}

    def fake_constructor(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return SimpleNamespace()

    monkeypatch.setattr(runner, "_ORIGINAL_RISK_POSITION", fake_constructor)
    runner._LAST_IOC_SUBMIT_WALL_MS = 123456
    runner._LAST_IOC_SUBMIT_MONO = 42.5

    runner._risk_position_from_submit("signal", "remote", 999999, 999.0, "rest")
    assert captured["args"][2] == 123456
    assert captured["args"][3] == 42.5


async def _async_value(value):
    return value


def _async_result(value):
    return _async_value(value)
