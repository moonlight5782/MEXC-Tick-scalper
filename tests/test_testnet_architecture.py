from pathlib import Path

from rich.console import Console

import mexc_tick_scalper.testnet as testnet_package
import mexc_tick_scalper.testnet_app as testnet_app
from mexc_tick_scalper.baseline_v1 import apply_baseline_v1


LEGACY_EXECUTION_IMPORTS = (
    "auto_discovery_testnet_xrp_fixed",
    "auto_discovery_testnet_xrp_profit_hold",
    "auto_discovery_testnet_xrp_runtime_diag",
)


def _source(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _args():
    args = testnet_app.build_parser().parse_args([])
    apply_baseline_v1(args)
    testnet_app._validate_operational_args(args)
    return args


def test_no_structured_testnet_module_references_legacy_execution_bridge():
    root = Path(testnet_package.__file__).resolve().parent
    offenders = []
    for path in root.glob("*.py"):
        text = _source(path)
        if any(name in text for name in LEGACY_EXECUTION_IMPORTS):
            offenders.append(path.name)
    assert offenders == []


def test_composition_root_does_not_import_legacy_execution_modules():
    path = Path(testnet_app.__file__).resolve()
    text = _source(path)
    assert all(name not in text for name in LEGACY_EXECUTION_IMPORTS)


def test_testnet_app_injects_readonly_and_trading_execution_separately():
    readonly_execution = object()
    trading_execution = object()

    app = testnet_app.TestnetApp(
        _args(),
        readonly_execution=readonly_execution,
        trading_execution=trading_execution,
        app_console=Console(force_terminal=False),
    )

    assert app.universe.execution_config is readonly_execution
    assert app.trading.execution_config is trading_execution
    assert app.universe.execution_config is not app.trading.execution_config


def test_trading_session_has_no_global_monkeypatch_bridge():
    source = _source(Path(testnet_package.__file__).resolve().parent / "session.py")
    assert "legacy_engine.SYMBOL" not in source
    assert "legacy_engine.LeadLagGate" not in source
    assert "DiagnosticLeadLagGate" not in source


def test_testnet_universe_never_loads_environment_or_live_private_auth():
    root = Path(testnet_package.__file__).resolve().parent
    text = _source(root / "universe.py")
    assert "from_env(" not in text
    assert "demo_from_env(" not in text
    assert "MEXC_WEB_TOKEN" not in text


def test_universe_service_receives_execution_dependency_explicitly():
    source = _source(Path(testnet_package.__file__).resolve().parent / "universe.py")
    assert "def __init__(self, console, execution_config" in source
    assert "self.execution_config = execution_config" in source


def test_trading_session_receives_execution_dependency_explicitly():
    source = _source(Path(testnet_package.__file__).resolve().parent / "session.py")
    assert "execution_config" in source
    assert "TestnetTradingEngine(" in source


def test_trading_engine_delegates_open_position_lifecycle():
    root = Path(testnet_package.__file__).resolve().parent
    engine = _source(root / "trading_engine.py")
    manager = _source(root / "position_manager.py")

    assert "PositionManager(" in engine
    assert "self.position_manager.manage_position(" in engine
    assert "self.position_manager.close_position(" in engine
    assert "def _manage_position(" not in engine
    assert "def _close_position(" not in engine
    assert "class PositionManager:" in manager
    assert "async def manage_position(" in manager
    assert "async def close_position(" in manager


def test_position_manager_does_not_own_discovery_or_entry_submission():
    source = _source(Path(testnet_package.__file__).resolve().parent / "position_manager.py")
    assert "LeadLagGate" not in source
    assert "PairSelector" not in source
    assert "open_ioc(" not in source
    assert "requested_notional(" not in source


def test_scanner_does_not_depend_on_prelive_or_execution_modules():
    root = Path(testnet_package.__file__).resolve().parent
    text = _source(root / "scanner.py")
    assert "prelive_" not in text
    assert "web_execution" not in text
    assert "open_ioc" not in text


def test_structured_app_has_no_obsolete_profit_runner_threshold():
    option_strings = {
        option
        for action in testnet_app.build_parser()._actions
        for option in action.option_strings
    }
    assert "--profit-runner-arm-bps" not in option_strings
