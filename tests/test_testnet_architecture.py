from pathlib import Path

import mexc_tick_scalper.testnet as testnet_package
import mexc_tick_scalper.testnet_app as testnet_app


LEGACY_EXECUTION_IMPORTS = (
    "auto_discovery_testnet_xrp_fixed",
    "auto_discovery_testnet_xrp_profit_hold",
    "auto_discovery_testnet_xrp_runtime_diag",
)


def _source(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_only_session_module_may_reference_legacy_execution_bridge():
    root = Path(testnet_package.__file__).resolve().parent
    offenders = []
    for path in root.glob("*.py"):
        if path.name == "session.py":
            continue
        text = _source(path)
        if any(name in text for name in LEGACY_EXECUTION_IMPORTS):
            offenders.append(path.name)
    assert offenders == []


def test_composition_root_does_not_import_legacy_execution_modules():
    path = Path(testnet_app.__file__).resolve()
    text = _source(path)
    assert all(name not in text for name in LEGACY_EXECUTION_IMPORTS)


def test_testnet_universe_never_loads_environment_or_live_private_auth():
    root = Path(testnet_package.__file__).resolve().parent
    text = _source(root / "universe.py")
    assert "from_env(" not in text
    assert "demo_from_env(" not in text
    assert "MEXC_WEB_TOKEN" not in text


def test_scanner_does_not_depend_on_prelive_or_execution_modules():
    root = Path(testnet_package.__file__).resolve().parent
    text = _source(root / "scanner.py")
    assert "prelive_" not in text
    assert "web_execution" not in text
    assert "open_ioc" not in text
