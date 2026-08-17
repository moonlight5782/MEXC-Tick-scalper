from mexc_tick_scalper.demo_baseline_v1_mirror import ENTRY_RE, EXIT_RE
from mexc_tick_scalper.prelive_100_trade_shadow import _parse_symbol_allowlist


def test_entry_parser_matches_frozen_runner_output():
    line = (
        "ENTRY BTW_USDT SHORT requested=$10000 filled=$698 (7.0%) "
        "spread=2.79bps slip=0.00bps residual=-11.18bps cost=4.82bps"
    )
    match = ENTRY_RE.search(line)
    assert match is not None
    assert match.group("symbol") == "BTW_USDT"
    assert match.group("side") == "SHORT"
    assert float(match.group("filled")) == 698.0


def test_exit_parser_matches_frozen_runner_output():
    line = "EXIT BANK_USDT mexc_catchup_convergence pnl +6.28bps +$0.44 hold 152ms"
    match = EXIT_RE.search(line)
    assert match is not None
    assert match.group("symbol") == "BANK_USDT"
    assert match.group("reason") == "mexc_catchup_convergence"


def test_symbol_allowlist_is_execution_only_and_normalized():
    assert _parse_symbol_allowlist("ethfi_usdt, BANK_USDT,ethfi_usdt") == {
        "ETHFI_USDT",
        "BANK_USDT",
    }


def test_empty_symbol_allowlist_preserves_full_frozen_universe():
    assert _parse_symbol_allowlist("") == set()
