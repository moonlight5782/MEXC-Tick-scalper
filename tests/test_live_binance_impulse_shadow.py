import pytest

from mexc_tick_scalper.live_binance_impulse_shadow import (
    LiveRttProbe,
    ShadowTrade,
    _entry_latency_allowed,
    _entry_price,
    _exit_price,
    _load_latency_samples,
    _scaled_requested_notional,
    _simulate_ioc_entry,
    _simulate_market_exit,
    _summary,
    build_parser,
)
from mexc_tick_scalper.microspread_feed import LiveBook


def test_shadow_prices_cross_spread_and_apply_conservative_slippage():
    book = LiveBook(bid=99.0, ask=101.0, recv_ms=1, exchange_ts_ms=1)

    assert _entry_price(book, 1, 1.0) == 101.0101
    assert _exit_price(book, 1, 1.0) == 98.9901
    assert _entry_price(book, -1, 1.0) == 98.9901
    assert _exit_price(book, -1, 1.0) == 101.0101


def test_shadow_summary_reports_zero_fee_statistics():
    common = dict(
        symbol="XRP_USDT",
        direction=1,
        signal_ms=1,
        entry_ms=2,
        exit_ms=3,
        entry_price=1.0,
        exit_price=1.0,
        impulse_bps=2.0,
        entry_spread_bps=1.0,
        mfe_bps=2.0,
        mae_bps=-1.0,
        hold_ms=1,
        signal_to_fill_ms=1,
        exit_decision_to_fill_ms=1,
        exit_reason="test",
    )
    rows = [
        ShadowTrade(pnl_bps=2.0, pnl_usdt=2.0, **common),
        ShadowTrade(pnl_bps=-1.0, pnl_usdt=-1.0, **common),
        ShadowTrade(pnl_bps=0.0, pnl_usdt=0.0, **common),
    ]

    stats = _summary(rows)

    assert stats == {
        "trades": 3,
        "wins": 1,
        "losses": 1,
        "flats": 1,
        "pnl_usdt": 1.0,
        "win_rate": 50.0,
        "profit_factor": 2.0,
    }


def test_demo_latency_trace_replays_only_completed_trade_rows(tmp_path):
    path = tmp_path / "excursions.csv"
    path.write_text(
        "event,signal_to_provisional_ms,signal_to_ioc_post_ms,ioc_confirmation_ms,ioc_post_roundtrip_ms\n"
        "demo_ioc_request,,,,\n"
        "demo_exit,682.365,,,363.239\n"
        "demo_exit,,19.022,667.032,347.172\n",
        encoding="utf-8",
    )

    samples = _load_latency_samples(path)

    assert len(samples) == 2
    assert samples[0].entry_ms == pytest.approx(682.365)
    assert samples[0].exit_ms == pytest.approx(363.239)
    assert samples[1].entry_ms == pytest.approx(686.054)
    assert samples[1].exit_ms == pytest.approx(347.172)


def test_live_rtt_probe_uses_recent_robust_half_rtt():
    probe = LiveRttProbe(symbol="LINK_USDT", interval_seconds=1.0)
    probe.samples_ms.extend([20.0, 30.0, 200.0])
    probe.last_sample_at = 100.0

    assert probe.median_rtt_ms == 30.0
    assert probe.current_one_way_ms(now=102.0, max_age_seconds=3.0) == 15.0
    assert probe.current_one_way_ms(now=104.0, max_age_seconds=3.0) is None


def test_entry_latency_gate_blocks_only_above_configured_budget():
    assert _entry_latency_allowed(estimated_ms=199.9, maximum_ms=200.0)
    assert _entry_latency_allowed(estimated_ms=200.0, maximum_ms=200.0)
    assert not _entry_latency_allowed(estimated_ms=200.1, maximum_ms=200.0)
    assert _entry_latency_allowed(estimated_ms=10_000.0, maximum_ms=0.0)


def test_entry_latency_gate_is_opt_in_for_backward_compatible_controls():
    parser = build_parser()

    assert parser.parse_args([]).max_estimated_entry_latency_ms == 0.0
    assert parser.parse_args(
        ["--max-estimated-entry-latency-ms", "200"]
    ).max_estimated_entry_latency_ms == 200.0


def test_depth_ioc_accepts_partial_fill_without_topping_up():
    book = LiveBook(
        bid=99.9,
        ask=100.0,
        recv_ms=1,
        exchange_ts_ms=1,
        bids=((99.9, 3.0), (99.8, 4.0)),
        asks=((100.0, 2.0), (100.02, 3.0), (100.20, 100.0)),
    )

    fill, limit_price = _simulate_ioc_entry(
        book,
        direction=1,
        requested_notional_usdt=1_000.0,
        contract_size=1.0,
        limit_offset_bps=5.0,
        slippage_bps=0.0,
    )

    assert limit_price == pytest.approx(100.05)
    assert fill.requested_base_qty == pytest.approx(1_000.0 / 100.05)
    assert fill.filled_base_qty == pytest.approx(5.0)
    assert fill.filled_notional_usdt == pytest.approx(500.06)
    assert fill.avg_price == pytest.approx(100.012)
    assert fill.fill_ratio == pytest.approx(0.50025)
    assert fill.levels_used == 2
    assert fill.available_base_qty == pytest.approx(5.0)
    assert fill.available_notional_usdt == pytest.approx(500.06)


def test_depth_market_exit_uses_vwap_and_reports_visible_shortfall():
    book = LiveBook(
        bid=99.9,
        ask=100.0,
        recv_ms=1,
        exchange_ts_ms=1,
        bids=((99.9, 2.0), (99.8, 3.0)),
        asks=((100.0, 10.0),),
    )

    fill = _simulate_market_exit(
        book,
        position_direction=1,
        base_qty=8.0,
        contract_size=1.0,
        slippage_bps=0.0,
    )

    assert fill.filled_base_qty == pytest.approx(5.0)
    assert fill.avg_price == pytest.approx(99.84)
    assert fill.fill_ratio == pytest.approx(0.625)
    assert fill.levels_used == 2
    assert fill.available_notional_usdt == pytest.approx(499.2)


def test_equity_scaling_compounds_but_respects_isolated_margin_cap():
    assert _scaled_requested_notional(
        base_notional_usdt=10_000.0,
        equity_usdt=60.0,
        initial_equity_usdt=60.0,
        leverage=200,
        max_margin_fraction=0.90,
        max_notional_usdt=0.0,
        enabled=True,
    ) == pytest.approx(10_000.0)
    assert _scaled_requested_notional(
        base_notional_usdt=10_000.0,
        equity_usdt=66.0,
        initial_equity_usdt=60.0,
        leverage=200,
        max_margin_fraction=0.90,
        max_notional_usdt=10_500.0,
        enabled=True,
    ) == pytest.approx(10_500.0)
    assert _scaled_requested_notional(
        base_notional_usdt=10_000.0,
        equity_usdt=10.0,
        initial_equity_usdt=60.0,
        leverage=100,
        max_margin_fraction=0.50,
        max_notional_usdt=0.0,
        enabled=True,
    ) == pytest.approx(500.0)
