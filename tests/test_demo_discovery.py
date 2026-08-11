from mexc_tick_scalper.demo_discovery import _contract_rows, build_parser


def test_contract_rows_accepts_demo_list_payload():
    payload = {
        "success": True,
        "data": [
            {"symbol": "BTC_USDT", "contractSize": 0.0001},
            {"symbol": "ETH_USDT", "contractSize": 0.01},
        ],
    }
    rows = _contract_rows(payload)
    assert [row["symbol"] for row in rows] == ["BTC_USDT", "ETH_USDT"]


def test_contract_rows_is_fail_closed_for_bad_payload():
    assert _contract_rows({"success": True, "data": "unexpected"}) == []
    assert _contract_rows(None) == []


def test_demo_discovery_cli_commands_parse():
    parser = build_parser()
    assert parser.parse_args(["contracts"]).command == "contracts"
    assert parser.parse_args(["scan"]).command == "scan"
