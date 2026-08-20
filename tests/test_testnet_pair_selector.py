from dataclasses import dataclass

import pytest

from mexc_tick_scalper.auto_discovery_testnet_selector import _select, _zero_fee_choice


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


def test_zero_fee_scope_defaults_to_strict_on_enter():
    assert _zero_fee_choice("") is True
    assert _zero_fee_choice("y") is True
    assert _zero_fee_choice("YES") is True
    assert _zero_fee_choice("да") is True


def test_zero_fee_scope_can_be_disabled():
    assert _zero_fee_choice("n") is False
    assert _zero_fee_choice("NO") is False
    assert _zero_fee_choice("нет") is False


def test_invalid_zero_fee_scope_is_rejected():
    with pytest.raises(ValueError):
        _zero_fee_choice("maybe")
