import pytest

from mexc_tick_scalper.live_binance_impulse_shadow import (
    LiveRttProbe,
    ShadowTrade,
    _entry_price,
    _exit_price,
    _load_latency_samples,
    _summary,
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
