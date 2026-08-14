from mexc_tick_scalper.live_lead_lag_scan import LeadLagStats, _chunks, _rank_key


def test_chunks_respects_shard_size():
    assert _chunks(["A", "B", "C", "D", "E"], 2) == [["A", "B"], ["C", "D"], ["E"]]


def test_stats_record_tracks_direction_and_edges():
    row = LeadLagStats("UNI_USDT", "UNIUSDT")
    row.record(direction=1, edge_bps=5.0, binance_move_bps=6.0, mexc_move_bps=1.0, age_ms=25.0, now_ms=1000)
    row.record(direction=-1, edge_bps=-7.0, binance_move_bps=-8.0, mexc_move_bps=-2.0, age_ms=30.0, now_ms=1400)

    assert row.events == 2
    assert row.long_events == 1
    assert row.short_events == 1
    assert row.avg_edge_bps == 6.0
    assert row.max_edge_bps == 7.0
    assert row.max_binance_move_bps == 8.0
    assert row.max_mexc_move_bps == 2.0
    assert row.min_age_ms == 25.0
    assert row.last_event_ms == 1400


def test_rank_prefers_more_events_then_larger_average_edge():
    frequent = LeadLagStats("A_USDT", "AUSDT", events=3, sum_edge_bps=12.0, max_edge_bps=5.0)
    sparse = LeadLagStats("B_USDT", "BUSDT", events=2, sum_edge_bps=20.0, max_edge_bps=12.0)
    assert _rank_key(frequent) < _rank_key(sparse)

    edge_a = LeadLagStats("C_USDT", "CUSDT", events=2, sum_edge_bps=10.0, max_edge_bps=6.0)
    edge_b = LeadLagStats("D_USDT", "DUSDT", events=2, sum_edge_bps=8.0, max_edge_bps=7.0)
    assert _rank_key(edge_a) < _rank_key(edge_b)
