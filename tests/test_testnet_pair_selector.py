from dataclasses import dataclass

import pytest

from mexc_tick_scalper.testnet.models import FeeScope
from mexc_tick_scalper.testnet.selector import PairSelector


@dataclass
class FakeRow:
    symbol: str


def rows():
    return [FakeRow("SOL_USDT"), FakeRow("XRP_USDT"), FakeRow("DOGE_USDT")]


def test_enter_selects_first_candidate():
    assert PairSelector.choose(rows(), "").symbol == "SOL_USDT"
    assert PairSelector.choose(rows(), "   ").symbol == "SOL_USDT"


def test_number_selects_ranked_candidate():
    assert PairSelector.choose(rows(), "1").symbol == "SOL_USDT"
    assert PairSelector.choose(rows(), "2").symbol == "XRP_USDT"
    assert PairSelector.choose(rows(), "3").symbol == "DOGE_USDT"


def test_symbol_can_be_selected_with_or_without_usdt_suffix():
    assert PairSelector.choose(rows(), "xrp").symbol == "XRP_USDT"
    assert PairSelector.choose(rows(), "DOGE_USDT").symbol == "DOGE_USDT"


def test_invalid_selection_is_rejected():
    with pytest.raises(ValueError):
        PairSelector.choose(rows(), "0")
    with pytest.raises(ValueError):
        PairSelector.choose(rows(), "99")
    with pytest.raises(ValueError):
        PairSelector.choose(rows(), "BTC")


def test_testnet_fee_scope_defaults_to_all():
    assert PairSelector.fee_scope("") is FeeScope.ALL
    assert PairSelector.fee_scope("a") is FeeScope.ALL
    assert PairSelector.fee_scope("ALL") is FeeScope.ALL
    assert PairSelector.fee_scope("да") is FeeScope.ALL


def test_zero_fee_scope_is_explicit_option():
    assert PairSelector.fee_scope("z") is FeeScope.ZERO_ONLY
    assert PairSelector.fee_scope("zero") is FeeScope.ZERO_ONLY
    assert PairSelector.fee_scope("0") is FeeScope.ZERO_ONLY


def test_invalid_fee_scope_is_rejected():
    with pytest.raises(ValueError):
        PairSelector.fee_scope("maybe")
