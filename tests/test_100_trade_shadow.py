from pathlib import Path

from mexc_tick_scalper.prelive_100_trade_shadow import build_parser


def test_default_target_is_100_closed_trades():
    args = build_parser().parse_args([])
    assert args.target_closed_trades == 100


def test_wrapper_is_structurally_paper_only():
    source = Path(__file__).parents[1] / "src" / "mexc_tick_scalper" / "prelive_100_trade_shadow.py"
    text = source.read_text(encoding="utf-8")
    assert "write_enabled=True" not in text
    assert "open_ioc(" not in text
    assert "close_market_reduce_only(" not in text
    assert "cancel_order(" not in text
