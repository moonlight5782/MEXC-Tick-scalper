from dataclasses import dataclass

import pytest

from mexc_tick_scalper.auto_discovery_testnet_selector import _select


@dataclass
class FakeRow:
    symbol: str


def rows():
    return [FakeRow("SOL_USDT"), FakeRow("XRP_USDT"), FakeRow("DOGE_USDT")]


def test_enter_selects_first_candidate():
    assert _select(rows(), "").symbol == "SOL_USDT"
    assert _select(rows(), "   ").symbol == "SOL_USDT"


def test_number_selects_ranked_candidate():
    assert _select(rows(), "1").symbol == "SOL_USDT"
    assert _select(rows(), "2").symbol == "XRP_USDT"
    assert _select(rows(), "3").symbol == "DOGE_USDT"


def test_symbol_can_be_selected_with_or_without_usdt_suffix():
    assert _select(rows(), "xrp").symbol == "XRP_USDT"
    assert _select(rows(), "DOGE_USDT").symbol == "DOGE_USDT"


def test_invalid_selection_is_rejected():
    with pytest.raises(ValueError):
        _select(rows(), "0")
    with pytest.raises(ValueError):
        _select(rows(), "99")
    with pytest.raises(ValueError):
        _select(rows(), "BTC")
