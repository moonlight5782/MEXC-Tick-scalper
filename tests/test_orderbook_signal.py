from mexc_tick_scalper.orderbook_signal import analyze_order_book, book_confirmation


def test_balanced_book_is_neutral():
    book = analyze_order_book(
        bids=[[100.0, 10.0], [99.9, 10.0]],
        asks=[[100.1, 10.0], [100.2, 10.0]],
        depth=2,
    )
    assert book.valid
    assert abs(book.imbalance) < 1e-12
    assert abs(book.microprice_edge_bps) < 1e-12
    assert book.direction == 0


def test_bid_heavy_book_points_up():
    book = analyze_order_book(
        bids=[[100.0, 40.0], [99.9, 30.0]],
        asks=[[100.1, 5.0], [100.2, 5.0]],
        depth=2,
    )
    assert book.direction == 1
    assert book.imbalance > 0
    assert book.microprice > book.mid
    assert book.confidence > 0


def test_ask_heavy_book_points_down():
    book = analyze_order_book(
        bids=[[100.0, 5.0], [99.9, 5.0]],
        asks=[[100.1, 40.0], [100.2, 30.0]],
        depth=2,
    )
    assert book.direction == -1
    assert book.imbalance < 0
    assert book.microprice < book.mid


def test_strong_book_disagreement_vetoes_trade_flow_entry():
    book = analyze_order_book(
        bids=[[100.0, 2.0]],
        asks=[[100.1, 30.0]],
    )
    decision = book_confirmation(trade_direction=1, book=book, veto_confidence=0.25)
    assert not decision.allowed
    assert decision.reason == "strong_book_disagreement"


def test_aligned_book_only_boosts_existing_trade_direction():
    book = analyze_order_book(
        bids=[[100.0, 30.0]],
        asks=[[100.1, 2.0]],
    )
    decision = book_confirmation(trade_direction=1, book=book, confirm_confidence=0.10)
    assert decision.allowed
    assert decision.reason == "book_confirmed"
    assert decision.confidence_multiplier > 1.0


def test_book_never_invents_direction_without_trade_signal():
    book = analyze_order_book(
        bids=[[100.0, 30.0]],
        asks=[[100.1, 2.0]],
    )
    decision = book_confirmation(trade_direction=0, book=book)
    assert not decision.allowed
    assert decision.reason == "no_trade_direction"
