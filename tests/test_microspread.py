import math
import csv
import asyncio
from types import SimpleNamespace

from mexc_tick_scalper.demo_microspread_test import (
    MicroCandidate,
    _append_excursion,
    _flatten_exact_demo_position,
    _find_demo_position,
    _marketable_demo_price,
    _open_demo_ioc_with_leverage_fallback,
    _required_edge,
)
from mexc_tick_scalper.microspread import MicroSpreadModel
from mexc_tick_scalper.microspread_feed import EventMexcDepthFeed, LiveBook
from mexc_tick_scalper.execution import OrderSide, PositionSnapshot
from mexc_tick_scalper.web_execution import MexcWebError
import mexc_tick_scalper.demo_microspread_test as runner


def px(base: float, bps: float) -> float:
    return base * math.exp(bps / 10_000.0)


def seed(model: MicroSpreadModel, *, until_ms: int = 3000, step_ms: int = 100) -> None:
    for ts in range(0, until_ms + 1, step_ms):
        model.update_binance(bid=99.99, ask=100.01, ts_ms=ts)
        model.update_mexc(bid=99.99, ask=100.01, ts_ms=ts)


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
    assert args.max_hold_seconds == 15.0
    assert args.max_demo_volume is False
    assert args.demo_ioc_cross_bps == 5.0
    assert args.exclude_symbols == ""
    assert args.include_symbols == ""
    assert args.demo_zero_fee_only is False


def test_demo_only_fee_gate_still_requires_demo_zero_fee():
    zero = SimpleNamespace(maker=0.0, taker=0.0)
    nonzero = SimpleNamespace(maker=0.0, taker=0.0001)

    assert runner._fee_gate_allows_entry(nonzero, zero, require_live_zero_fee=False)
    assert not runner._fee_gate_allows_entry(zero, nonzero, require_live_zero_fee=False)
    assert not runner._fee_gate_allows_entry(nonzero, zero, require_live_zero_fee=True)


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
