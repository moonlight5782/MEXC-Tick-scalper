import math

from mexc_tick_scalper.live_lead_lag_shadow import (
    MexcBestBookFeed,
    PositiveTrailing,
    _candidate_score,
    _signed_move_bps,
)


def test_positive_trailing_only_ratchets_up_in_profit():
    trail = PositiveTrailing(distance_bps=1.5)
    assert trail.update(2.9) is None
    assert trail.update(3.0) == 0.5
    assert trail.update(4.0) == 0.5
    assert trail.update(5.0) == 2.0
    assert math.isclose(trail.update(6.0), 4.5)
    assert math.isclose(trail.update(8.0), 6.5)
    assert math.isclose(trail.update(7.0), 6.5)


def test_signed_move_uses_executable_direction():
    assert math.isclose(_signed_move_bps(1, 100.0, 100.1), 10.0)
    assert math.isclose(_signed_move_bps(-1, 100.0, 99.9), 10.0)
    assert _signed_move_bps(1, 100.0, 99.9) < 0
    assert _signed_move_bps(-1, 100.0, 100.1) < 0


def test_candidate_score_rewards_net_edge_and_repeatability():
    strong_repeatable = _candidate_score(11, 13.882, 1.071)
    wide_spread = _candidate_score(40, 15.619, 15.049)
    assert strong_repeatable > wide_spread


def test_depth_full_payload_parses_best_executable_prices():
    parsed = MexcBestBookFeed.parse_payload(
        {
            "channel": "push.depth.full",
            "symbol": "VELVET_USDT",
            "ts": 1_800_000_000_000,
            "data": {
                "bids": [[1.001, 5, 1], [1.000, 10, 1]],
                "asks": [[1.003, 6, 1], [1.004, 7, 1]],
            },
        }
    )
    assert parsed is not None
    symbol, book = parsed
    assert symbol == "VELVET_USDT"
    assert book.bid == 1.001
    assert book.ask == 1.003
    assert book.spread_bps > 0


def test_depth_payload_ignores_non_depth_messages():
    assert MexcBestBookFeed.parse_payload({"channel": "pong", "data": 1}) is None
