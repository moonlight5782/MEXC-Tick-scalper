import math
import csv
import asyncio
from types import SimpleNamespace

import pytest

from mexc_tick_scalper.demo_microspread_test import (
    MicroCandidate,
    ZERO_FEE_GROSS_CANDIDATE_V1,
    _apply_strategy_profile,
    _adverse_cut_for_leverage,
    _append_residual_sample,
    _append_excursion,
    _confirmed_candidate,
    _convergence_exit_allowed,
    _cycle_margin_usdt,
    _dynamic_sizing_ready,
    _flatten_exact_demo_position,
    _find_demo_position,
    _history_reconciled_fill,
    _estimated_demo_net_bps,
    _legacy_convergence_exit_allowed,
    _legacy_reversal_exit_allowed,
    _marketable_demo_price,
    _nonpositive_timeout_allowed,
    _open_demo_ioc_with_leverage_fallback,
    _profitable_reversal_exit_allowed,
    _read_fee_pair_fail_closed,
    _required_edge,
)
from mexc_tick_scalper.microspread import BinanceImpulseModel, MicroSpreadModel, MicroSpreadSnapshot
from mexc_tick_scalper.microspread_feed import EventMexcDepthFeed, LiveBook
from mexc_tick_scalper.execution import OrderSide, PositionSnapshot
from mexc_tick_scalper.web_execution import MexcWebError
import mexc_tick_scalper.demo_microspread_test as runner


def px(base: float, bps: float) -> float:
    return base * math.exp(bps / 10_000.0)


def test_zero_fee_gross_candidate_profile_is_frozen_and_reproducible():
    args = runner.build_parser().parse_args([
        "--strategy-profile", "binance-impulse-zero-fee-gross-v1",
        "--include-symbols", "XAUT_USDT",
        "--leverage", "1",
    ])

    applied = _apply_strategy_profile(args)

    for name, value in ZERO_FEE_GROSS_CANDIDATE_V1.items():
        assert getattr(applied, name) == value
    assert applied.entry_signal_source == "binance-impulse"
    assert applied.signal_mexc_source == "demo"
    assert applied.allow_demo_fee_accounting is True
    assert applied.demo_zero_fee_only is False


def seed(model: MicroSpreadModel, *, until_ms: int = 3000, step_ms: int = 100) -> None:
    for ts in range(0, until_ms + 1, step_ms):
        model.update_binance(bid=99.99, ask=100.01, ts_ms=ts)
        model.update_mexc(bid=99.99, ask=100.01, ts_ms=ts)


def test_binance_impulse_entry_ignores_mexc_direction():
    model = BinanceImpulseModel(horizon_ms=100, min_edge_bps=1.0)
    model.update_binance(bid=99.99, ask=100.01, ts_ms=0)
    model.update_mexc(bid=199.9, ask=200.1, ts_ms=0)
    model.update_binance(bid=100.01, ask=100.03, ts_ms=100)
    model.update_mexc(bid=149.9, ask=150.1, ts_ms=100)

    snap = model.signal(now_ms=100, threshold_bps=1.0)

    assert snap.ready
    assert snap.direction == 1
    assert snap.binance_move_bps > 1.0
    assert snap.reason == "binance_impulse_confirmed"


def test_binance_impulse_hysteresis_blocks_duplicate_signal():
    model = BinanceImpulseModel(horizon_ms=100, min_edge_bps=1.0)
    model.update_binance(bid=99.99, ask=100.01, ts_ms=0)
    model.update_binance(bid=100.01, ask=100.03, ts_ms=100)

    assert model.signal(now_ms=100, threshold_bps=1.0).ready
    assert not model.signal(now_ms=100, threshold_bps=1.0).ready


def test_sub_one_bps_microspread_can_signal():
    model = MicroSpreadModel(
        horizon_ms=100,
        baseline_seconds=8,
        baseline_exclusion_ms=1000,
        min_edge_bps=0.35,
        min_binance_move_bps=0.02,
        max_binance_age_ms=300,
        max_mexc_age_ms=2000,
    )
    seed(model)

    moved = px(100.0, 0.60)
    model.update_binance(bid=moved - 0.01, ask=moved + 0.01, ts_ms=3100)
    snap = model.signal(now_ms=3100, threshold_bps=0.40)

    assert snap.ready is True
    assert snap.direction == 1
    assert 0.45 <= snap.edge_bps <= 0.75
    assert snap.binance_move_bps > 0.02


def test_same_excursion_fires_once_then_rearms_after_convergence():
    model = MicroSpreadModel(
        horizon_ms=100,
        baseline_seconds=8,
        baseline_exclusion_ms=1000,
        min_edge_bps=0.35,
        min_binance_move_bps=0.02,
    )
    seed(model)

    moved = px(100.0, 0.70)
    model.update_binance(bid=moved - 0.01, ask=moved + 0.01, ts_ms=3100)
    first = model.signal(now_ms=3100, threshold_bps=0.40)
    second = model.signal(now_ms=3110, threshold_bps=0.40)
    assert first.ready is True
    assert second.ready is False
    assert second.reason == "microspread_not_rearmed"

    model.update_mexc(bid=moved - 0.01, ask=moved + 0.01, ts_ms=3200)
    converged = model.signal(now_ms=3200, threshold_bps=0.40)
    assert converged.ready is False
    assert abs(converged.edge_bps) < 0.20

    moved2 = px(moved, 0.70)
    model.update_binance(bid=moved2 - 0.01, ask=moved2 + 0.01, ts_ms=3300)
    third = model.signal(now_ms=3300, threshold_bps=0.40)
    assert third.ready is True
    assert third.direction == 1


def test_mexc_quote_can_be_older_than_250ms_while_binance_must_be_fresh():
    model = MicroSpreadModel(
        horizon_ms=100,
        baseline_seconds=8,
        baseline_exclusion_ms=1000,
        min_edge_bps=0.35,
        min_binance_move_bps=0.02,
        max_binance_age_ms=300,
        max_mexc_age_ms=2000,
    )
    seed(model, until_ms=3000)

    moved = px(100.0, 0.65)
    model.update_binance(bid=moved - 0.01, ask=moved + 0.01, ts_ms=3800)
    snap = model.snapshot(now_ms=3800, threshold_bps=0.40)
    assert snap.mexc_age_ms == 800
    assert snap.binance_age_ms == 0
    assert snap.reason != "stale_mexc"

    stale = model.snapshot(now_ms=4200, threshold_bps=0.40)
    assert stale.reason == "stale_binance"


def test_live_spread_sets_executable_micro_threshold():
    assert math.isclose(
        _required_edge(spread_bps=0.20, min_edge_bps=0.35, min_net_edge_bps=0.20, spread_ratio=1.05),
        0.40,
    )
    assert math.isclose(
        _required_edge(spread_bps=1.00, min_edge_bps=0.35, min_net_edge_bps=0.20, spread_ratio=1.05),
        1.20,
    )


def test_marketable_demo_price_rounds_outward_to_contract_tick():
    assert _marketable_demo_price(OrderSide.LONG, 8.829, 5.0, 0.001) == 8.834
    assert _marketable_demo_price(OrderSide.SHORT, 8.829, 5.0, 0.001) == 8.824


def test_microspread_runner_imports():
    import mexc_tick_scalper.demo_microspread_test as runner

    parser = runner.build_parser()
    args = parser.parse_args([])
    assert args.min_edge_bps == 0.35
    assert args.min_binance_move_bps == 0.02
    assert args.max_hold_seconds == 0.0
    assert args.convergence_fraction == 0.0
    assert args.min_exit_profit_bps == 0.5
    assert args.binance_reversal_exit_bps == 0.0
    assert args.max_demo_volume is False
    assert args.demo_ioc_cross_bps == 5.0
    assert args.exclude_symbols == ""
    assert args.include_symbols == ""
    assert args.demo_zero_fee_only is False
    assert args.allow_demo_fee_accounting is False
    assert args.signal_mexc_source == "live"
    assert args.entry_confirm_ms == 0
    assert args.strategy_bankroll_usdt == 60.0
    assert args.target_notional_usdt == 0.0
    assert args.target_exposure_equity_multiple == 0.0
    assert args.sizing_activation_trades == 0
    assert args.sizing_min_profit_factor == 1.2
    assert args.adverse_cut_roe_pct == 0.0
    assert args.max_nonpositive_hold_seconds == 0.0
    assert args.residual_sample_ms == 100


def test_residual_csv_records_sub_threshold_observation(tmp_path):
    path = tmp_path / "residuals.csv"
    snap = MicroSpreadSnapshot(
        ready=False, direction=1, edge_bps=0.42, raw_gap_bps=3.2,
        baseline_gap_bps=2.78, binance_move_bps=0.11, mexc_move_bps=0.01,
        binance_mid=100.0, mexc_mid=99.97, age_ms=12.0, binance_age_ms=5.0,
        mexc_age_ms=12.0, threshold_bps=1.0, reason="microspread_below_threshold",
    )
    _append_residual_sample(
        path, timestamp_ms=123, symbol="XAUT_USDT", signal_source="demo",
        snapshot=snap, threshold_bps=1.2, spread_bps=0.6, demo_book_age_ms=12.0,
    )
    rows = list(csv.DictReader(path.open(encoding="utf-8")))
    assert rows[0]["residual_bps"] == "0.420000000"
    assert rows[0]["reason"] == "microspread_below_threshold"
    assert rows[0]["ready"] == "0"


def test_demo_signal_source_is_explicit_parser_mode():
    args = runner.build_parser().parse_args([
        "--signal-mexc-source", "demo",
        "--demo-zero-fee-only",
        "--include-symbols", "XAUT_USDT",
    ])

    assert args.signal_mexc_source == "demo"
    assert args.demo_zero_fee_only is True
    assert args.include_symbols == "XAUT_USDT"


def test_entry_confirmation_rejects_one_tick_flash_and_accepts_persistent_edge():
    candidate = SimpleNamespace(symbol="XAUT_USDT", direction=1)

    ready, pending = _confirmed_candidate(None, candidate, now_ms=1_000, confirm_ms=100)
    assert ready is False
    ready, pending = _confirmed_candidate(pending, None, now_ms=1_050, confirm_ms=100)
    assert ready is False
    assert pending is None

    ready, pending = _confirmed_candidate(None, candidate, now_ms=2_000, confirm_ms=100)
    assert ready is False
    ready, pending = _confirmed_candidate(pending, candidate, now_ms=2_099, confirm_ms=100)
    assert ready is False
    ready, _ = _confirmed_candidate(pending, candidate, now_ms=2_100, confirm_ms=100)
    assert ready is True


def test_old_bot_exposure_profile_scales_margin_by_contract_leverage():
    assert _cycle_margin_usdt(
        fixed_margin_usdt=0.1, strategy_equity_usdt=60.0,
        target_exposure_multiple=10.6, leverage=1000,
    ) == pytest.approx(0.636)
    assert _cycle_margin_usdt(
        fixed_margin_usdt=0.1, strategy_equity_usdt=60.0,
        target_exposure_multiple=10.6, leverage=200,
    ) == pytest.approx(3.18)


def test_explicit_target_notional_overrides_probation_and_equity_sizing():
    assert _cycle_margin_usdt(
        fixed_margin_usdt=0.1, strategy_equity_usdt=60.0,
        target_exposure_multiple=10.6, leverage=1000, target_notional_usdt=10_000.0,
    ) == pytest.approx(10.0)
    assert _cycle_margin_usdt(
        fixed_margin_usdt=0.1, strategy_equity_usdt=60.0,
        target_exposure_multiple=10.6, leverage=200, target_notional_usdt=10_000.0,
    ) == pytest.approx(50.0)


def test_fee_pair_network_failure_is_fail_closed(monkeypatch):
    calls = 0

    async def failing_provider(adapter):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("temporary DNS failure")
        return object()

    monkeypatch.setattr(runner, "read_web_fee_provider", failing_provider)
    live, demo, error = asyncio.run(_read_fee_pair_fail_closed(object(), object()))
    assert live is None
    assert demo is None
    assert error == "OSError"


def test_adverse_cut_is_normalized_to_margin_roe_but_covers_spread():
    assert _adverse_cut_for_leverage(
        leverage=1000, spread_bps=0.23, fixed_cut_bps=1.5,
        spread_multiple=1.25, adverse_roe_pct=6.0,
    ) == pytest.approx(0.6)
    assert _adverse_cut_for_leverage(
        leverage=1000, spread_bps=0.60, fixed_cut_bps=1.5,
        spread_multiple=1.25, adverse_roe_pct=6.0,
    ) == pytest.approx(0.75)


def test_nonpositive_timeout_never_cuts_a_profitable_position():
    assert not _nonpositive_timeout_allowed(
        age_seconds=120.0, timeout_seconds=30.0, demo_net_bps=0.01,
    )
    assert _nonpositive_timeout_allowed(
        age_seconds=30.0, timeout_seconds=30.0, demo_net_bps=0.0,
    )
    assert not _nonpositive_timeout_allowed(
        age_seconds=29.9, timeout_seconds=30.0, demo_net_bps=-1.0,
    )


def test_old_bot_sizing_activates_only_after_positive_probation_pf():
    assert not _dynamic_sizing_ready(
        completed_trades=19, profit_usdt=1.0, loss_usdt=0.1,
        activation_trades=20, min_profit_factor=1.2,
    )
    assert not _dynamic_sizing_ready(
        completed_trades=20, profit_usdt=1.0, loss_usdt=0.9,
        activation_trades=20, min_profit_factor=1.2,
    )
    assert _dynamic_sizing_ready(
        completed_trades=20, profit_usdt=1.2, loss_usdt=0.9,
        activation_trades=20, min_profit_factor=1.2,
    )


def test_demo_net_mark_subtracts_both_fees_before_trailing():
    gross, net, net_bps = _estimated_demo_net_bps(
        direction=1,
        entry_price=100.0,
        exit_price=100.05,
        qty=1.0,
        entry_fee_usdt=0.02,
        exit_fee_rate=0.0002,
    )

    assert gross == pytest.approx(0.05)
    assert net == pytest.approx(0.00999)
    assert net_bps == pytest.approx(0.999)


def test_demo_net_mark_is_symmetric_for_short_positions():
    long_result = _estimated_demo_net_bps(
        direction=1, entry_price=100.0, exit_price=100.1, qty=2.0,
        entry_fee_usdt=0.0, exit_fee_rate=0.0,
    )
    short_result = _estimated_demo_net_bps(
        direction=-1, entry_price=100.0, exit_price=99.9, qty=2.0,
        entry_fee_usdt=0.0, exit_fee_rate=0.0,
    )

    assert long_result[2] == pytest.approx(short_result[2])


def test_convergence_does_not_exit_before_prices_nearly_match():
    assert not _convergence_exit_allowed(
        current_edge_bps=40.0,
        entry_edge_bps=200.0,
        convergence_bps=0.1,
        convergence_fraction=0.0,
        demo_net_bps=100.0,
        min_exit_profit_bps=0.5,
    )


def test_convergence_does_not_lock_in_negative_demo_net():
    assert not _convergence_exit_allowed(
        current_edge_bps=0.05,
        entry_edge_bps=2.0,
        convergence_bps=0.1,
        convergence_fraction=0.0,
        demo_net_bps=-0.1,
        min_exit_profit_bps=0.5,
    )
    assert _convergence_exit_allowed(
        current_edge_bps=0.05,
        entry_edge_bps=2.0,
        convergence_bps=0.1,
        convergence_fraction=0.0,
        demo_net_bps=0.5,
        min_exit_profit_bps=0.5,
    )


def test_reversal_exit_ignores_noise_and_never_locks_in_negative_net():
    common = dict(
        current_direction=-1,
        position_direction=1,
        entry_threshold_bps=1.2,
        reversal_edge_bps=0.2,
        min_exit_profit_bps=0.5,
    )

    assert not _profitable_reversal_exit_allowed(
        current_edge_bps=-0.3, demo_net_bps=2.0, **common,
    )
    assert not _profitable_reversal_exit_allowed(
        current_edge_bps=-2.0, demo_net_bps=-0.1, **common,
    )
    assert _profitable_reversal_exit_allowed(
        current_edge_bps=-2.0, demo_net_bps=0.5, **common,
    )


def test_legacy_convergence_reproduces_54b789e_without_demo_profit_gate():
    assert _legacy_convergence_exit_allowed(
        current_edge_bps=0.3,
        entry_edge_bps=2.0,
        convergence_bps=0.1,
        convergence_fraction=0.2,
    )
    assert not _legacy_convergence_exit_allowed(
        current_edge_bps=0.5,
        entry_edge_bps=2.0,
        convergence_bps=0.1,
        convergence_fraction=0.2,
    )


def test_legacy_reversal_reproduces_54b789e_threshold():
    assert _legacy_reversal_exit_allowed(
        current_direction=-1,
        position_direction=1,
        current_edge_bps=-0.3,
        reversal_edge_bps=0.2,
    )
    assert not _legacy_reversal_exit_allowed(
        current_direction=1,
        position_direction=1,
        current_edge_bps=2.0,
        reversal_edge_bps=0.2,
    )


def test_demo_only_fee_gate_still_requires_demo_zero_fee():
    zero = SimpleNamespace(maker=0.0, taker=0.0)
    nonzero = SimpleNamespace(maker=0.0, taker=0.0001)

    assert runner._fee_gate_allows_entry(nonzero, zero, require_live_zero_fee=False)
    assert not runner._fee_gate_allows_entry(zero, nonzero, require_live_zero_fee=False)
    assert not runner._fee_gate_allows_entry(nonzero, zero, require_live_zero_fee=True)


def test_demo_fee_accounting_mode_requires_live_zero_fee_but_accepts_measured_demo_fee():
    zero = SimpleNamespace(maker=0.0, taker=0.0)
    nonzero = SimpleNamespace(maker=0.0, taker=0.0002)

    assert runner._fee_gate_allows_entry(
        zero, nonzero, require_live_zero_fee=True, require_demo_zero_fee=False,
    )
    assert not runner._fee_gate_allows_entry(
        nonzero, nonzero, require_live_zero_fee=True, require_demo_zero_fee=False,
    )


def test_discovery_requires_demo_and_live_zero_fee(monkeypatch):
    live_a = SimpleNamespace(mexc_symbol="A_USDT")
    live_b = SimpleNamespace(mexc_symbol="B_USDT")

    class FakeAdapter:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

    class DemoFees:
        def status(self, symbol):
            return SimpleNamespace(
                maker=0.0,
                taker=0.0 if symbol == "A_USDT" else 0.0002,
            )

    async def live_rows():
        return [live_a, live_b]

    async def demo_rows(adapter):
        return [{"symbol": "A_USDT"}, {"symbol": "B_USDT"}]

    async def demo_fees(adapter):
        return DemoFees()

    monkeypatch.setattr(runner, "discover_live_zero_fee_crosslisted", live_rows)
    monkeypatch.setattr(runner, "_fetch_contracts", demo_rows)
    monkeypatch.setattr(runner, "read_web_fee_provider", demo_fees)
    monkeypatch.setattr(runner.WebExecutionConfig, "demo_from_env", lambda **kwargs: object())
    monkeypatch.setattr(runner, "MexcWebExecutionAdapter", lambda config: FakeAdapter())

    rows = asyncio.run(runner._discover_intersection())

    assert [row.live.mexc_symbol for row in rows] == ["A_USDT"]


def test_full_depth_channel_is_parsed():
    parsed = EventMexcDepthFeed._parse_book({
        "channel": "push.depth.full",
        "symbol": "BCH_USDT",
        "ts": 1_786_700_000_000,
        "data": {"bids": [["100.00", "2"]], "asks": [["100.01", "3"]]},
    }, 1_786_700_000_123)

    assert parsed is not None
    symbol, book = parsed
    assert symbol == "BCH_USDT"
    assert book.bid == 100.0
    assert book.ask == 100.01


def test_excursion_csv_preserves_sub_one_bps_residual(tmp_path):
    model = MicroSpreadModel(min_edge_bps=0.35)
    seed(model)
    moved = px(100.0, 0.60)
    model.update_binance(bid=moved - 0.01, ask=moved + 0.01, ts_ms=3100)
    snap = model.signal(now_ms=3100, threshold_bps=0.40)
    candidate = MicroCandidate(
        symbol="BCH_USDT", direction=snap.direction, edge_bps=snap.edge_bps,
        threshold_bps=snap.threshold_bps,
        net_margin_bps=abs(snap.edge_bps) - snap.threshold_bps,
        spread_bps=0.20, binance_move_bps=snap.binance_move_bps,
        mexc_move_bps=snap.mexc_move_bps,
        book=LiveBook(99.99, 100.01, 3100, 3100), snapshot=snap,
    )
    path = tmp_path / "excursions.csv"

    _append_excursion(
        path, candidate, timestamp_ms=3100, excursion_id="exc-1", event="demo_exit",
        live_pnl_usdt=0.001, move_bps=0.5, mfe_bps=0.8, mae_bps=-0.2,
        exit_reason="microspread_converged", hold_ms=125.0,
        demo_entry_fee_usdt=0.002, demo_exit_fee_usdt=0.003,
        demo_gross_pnl_usdt=0.010, demo_net_pnl_usdt=0.005,
        entry_timing={
            "signal_detected_ms": 1000.0,
            "demo_book_lookup_start_ms": 1001.0,
            "demo_book_lookup_end_ms": 1001.2,
            "ioc_call_start_ms": 1002.0,
            "ioc_post_start_ms": 1003.0,
            "ioc_post_response_ms": 1103.0,
            "ioc_confirmed_ms": 1903.0,
            "reconciliation_start_ms": 1904.0,
            "provisional_started_ms": 1903.5,
            "position_visible_ms": 1914.0,
        },
    )

    with path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == 1
    assert rows[0]["symbol"] == "BCH_USDT"
    assert rows[0]["event"] == "demo_exit"
    assert rows[0]["excursion_id"] == "exc-1"
    assert rows[0]["exit_reason"] == "microspread_converged"
    assert float(rows[0]["live_pnl_usdt"]) == 0.001
    assert float(rows[0]["demo_entry_fee_usdt"]) == 0.002
    assert float(rows[0]["demo_exit_fee_usdt"]) == 0.003
    assert float(rows[0]["demo_gross_pnl_usdt"]) == 0.010
    assert float(rows[0]["demo_net_pnl_usdt"]) == 0.005
    assert float(rows[0]["signal_to_ioc_post_ms"]) == 3.0
    assert float(rows[0]["ioc_post_roundtrip_ms"]) == 100.0
    assert float(rows[0]["ioc_confirmation_ms"]) == 900.0
    assert float(rows[0]["reconciliation_ms"]) == 10.0
    assert float(rows[0]["signal_to_provisional_ms"]) == 903.5
    assert float(rows[0]["signal_to_position_visible_ms"]) == 914.0
    assert 0.0 < abs(float(rows[0]["residual_bps"])) < 1.0


def test_depth_feed_can_cache_books_without_mutating_a_strategy_model():
    feed = EventMexcDepthFeed(["XPL_USDT"], None, __import__("asyncio").Event())
    assert feed.models == {}


def test_demo_ioc_retries_explicit_risk_tier_rejection_at_lower_leverage():
    class TieredAdapter:
        def __init__(self):
            self.leverages = []

        async def open_ioc(self, **kwargs):
            self.leverages.append(kwargs["leverage"])
            if kwargs["leverage"] > 12:
                raise MexcWebError("code=8819 message=Exceeded maximum contracts")
            return kwargs["qty"]

    adapter = TieredAdapter()
    fill, leverage = asyncio.run(_open_demo_ioc_with_leverage_fallback(
        adapter,
        symbol="RAVE_USDT",
        side=OrderSide.LONG,
        price=1.0,
        min_base_qty=1.0,
        target_margin_usdt=0.1,
        leverage_cap=50,
    ))

    assert adapter.leverages == [50, 25, 12]
    assert leverage == 12
    assert math.isclose(fill, 1.2)


def test_demo_ioc_skips_symbol_when_every_leverage_tier_is_rejected():
    class RejectedAdapter:
        async def open_ioc(self, **kwargs):
            raise MexcWebError("code=8819 message=Exceeded maximum contracts")

    fill, leverage = asyncio.run(_open_demo_ioc_with_leverage_fallback(
        RejectedAdapter(), symbol="RAVE_USDT", side=OrderSide.SHORT,
        price=1.0, min_base_qty=1.0, target_margin_usdt=0.1, leverage_cap=4,
    ))

    assert fill is None
    assert leverage == 0


def test_max_demo_volume_reserves_round_trip_testnet_fees():
    class RecordingAdapter:
        async def open_ioc(self, **kwargs):
            self.order = kwargs
            return kwargs["qty"]

    adapter = RecordingAdapter()
    fill, leverage = asyncio.run(_open_demo_ioc_with_leverage_fallback(
        adapter, symbol="XRP_USDT", side=OrderSide.LONG,
        price=1.0, min_base_qty=1.0, target_margin_usdt=0.1,
        leverage_cap=300, available_margin_usdt=10_000.0,
    ))

    notional = adapter.order["qty"] * adapter.order["price"]
    required_with_round_trip_fees = notional / leverage + notional * 0.0004
    assert leverage == 300
    assert fill == adapter.order["qty"]
    assert required_with_round_trip_fees <= 9_800.0 + 1e-9


def test_demo_ioc_can_skip_synchronous_position_visibility_wait():
    class RecordingAdapter:
        async def open_ioc(self, **kwargs):
            self.order = kwargs
            return kwargs["qty"]

    adapter = RecordingAdapter()
    asyncio.run(_open_demo_ioc_with_leverage_fallback(
        adapter, symbol="LINK_USDT", side=OrderSide.LONG,
        price=10.0, min_base_qty=1.0, target_margin_usdt=1.0,
        leverage_cap=10, wait_for_visibility=False,
    ))

    assert adapter.order["wait_for_visibility"] is False


def test_demo_ioc_classifies_insufficient_balance_without_retrying():
    class EmptyAdapter:
        def __init__(self):
            self.calls = 0

        async def open_ioc(self, **kwargs):
            self.calls += 1
            raise MexcWebError("code=2005 message=Balance insufficient")

    adapter = EmptyAdapter()
    fill, leverage = asyncio.run(_open_demo_ioc_with_leverage_fallback(
        adapter, symbol="XRP_USDT", side=OrderSide.SHORT,
        price=1.0, min_base_qty=1.0, target_margin_usdt=0.1,
        leverage_cap=300, available_margin_usdt=100.0,
    ))

    assert fill is None
    assert leverage == -1
    assert adapter.calls == 1


def test_exact_position_exit_reconciles_by_position_id(monkeypatch):
    target = PositionSnapshot(
        symbol="ARB_USDT", side=OrderSide.SHORT, qty=1.0, entry_price=100.0,
        leverage=10, isolated=True, position_id="short-1",
    )
    opposite = PositionSnapshot(
        symbol="ARB_USDT", side=OrderSide.LONG, qty=2.0, entry_price=100.0,
        leverage=10, isolated=True, position_id="long-2",
    )

    class HedgeAdapter:
        def __init__(self):
            self.closed = []

        async def close_position_snapshot_reduce_only(self, position, *, client_order_id):
            self.closed.append(position.position_id)
            return "closed"

        async def get_positions(self, symbol):
            return [opposite]

    adapter = HedgeAdapter()
    result = asyncio.run(_flatten_exact_demo_position(adapter, target, "test"))

    assert result == "closed"
    assert adapter.closed == ["short-1"]


def test_position_history_replaces_stale_close_price_and_splits_total_fee():
    target = PositionSnapshot(
        symbol="XRP_USDT", side=OrderSide.LONG, qty=30.0, entry_price=0.9997,
        leverage=300, isolated=True, position_id="31601275",
    )

    class HistoryAdapter:
        async def _request(self, method, path, params):
            return {"data": {"resultList": [{
                "positionId": 31601275,
                "closeAvgPrice": 0.9993,
                "totalFee": 0.011994,
            }]}}

    fill = asyncio.run(_history_reconciled_fill(
        HistoryAdapter(), target, None, entry_fee_usdt=0.0059982,
    ))

    assert fill.avg_price == pytest.approx(0.9993)
    assert fill.fee_usdt == pytest.approx(0.0059958)
    assert fill.position_id == "31601275"


def test_position_lookup_selects_requested_hedge_leg():
    short = PositionSnapshot(
        symbol="ARB_USDT", side=OrderSide.SHORT, qty=1.0, entry_price=100.0,
        leverage=10, isolated=True, position_id="short-1",
    )
    long = PositionSnapshot(
        symbol="ARB_USDT", side=OrderSide.LONG, qty=1.0, entry_price=100.0,
        leverage=10, isolated=True, position_id="long-1",
    )

    class Adapter:
        async def get_positions(self, symbol):
            return [short, long]

    assert asyncio.run(_find_demo_position(Adapter(), "ARB_USDT", OrderSide.LONG)) is long
