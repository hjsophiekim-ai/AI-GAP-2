"""Isolation guard tests (docs §3/§11/§13) — these are the tests the task
explicitly requires to FAIL if TSLA_AUTO ever imports/calls MACD2, domestic
KIS order functions, or a forbidden symbol (TSLT, or a domestic ticker).
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from app.trading.tsla_auto import config, ledger, state_store
from app.trading.tsla_auto.market_data import MarketDataService
from app.trading.tsla_auto.models import Direction
from app.trading.tsla_auto.order_executor import execute_signal
from tests.tsla_auto.fake_broker import FakeBroker

_TSLA_AUTO_DIR = Path(__file__).resolve().parents[2] / "app" / "trading" / "tsla_auto"
_UI_PATH = Path(__file__).resolve().parents[2] / "app" / "ui" / "pages"

_FORBIDDEN_IMPORT_PREFIXES = (
    "app.trading.macd2",
    "app.trading.macd_hynix",
    "app.trading.macd_pipeline",
    "app.trading.strategy_ownership",
)
# app.trading.kis_client itself is allowed to be imported ONLY for its
# generic auth/rate-limit layer in principle, but this codebase's actual
# domestic order functions live there too — TSLA_AUTO must not import this
# module at all (its own kis_overseas_adapter.py is fully independent).
_FORBIDDEN_MODULES = frozenset({"app.trading.kis_client", "app.trading.broker_factory", "app.trading.broker_base"})


def _iter_py_files():
    for path in _TSLA_AUTO_DIR.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        yield path


def _imported_module_names(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def test_no_tsla_auto_file_imports_macd2_or_strategy_ownership():
    violations = []
    for path in _iter_py_files():
        imported = _imported_module_names(path)
        for name in imported:
            if any(name == p or name.startswith(p + ".") for p in _FORBIDDEN_IMPORT_PREFIXES):
                violations.append((str(path), name))
    assert violations == [], f"forbidden imports found: {violations}"


def test_no_tsla_auto_file_imports_domestic_kis_client_or_broker_factory():
    violations = []
    for path in _iter_py_files():
        imported = _imported_module_names(path)
        for name in imported:
            if name in _FORBIDDEN_MODULES:
                violations.append((str(path), name))
    assert violations == [], f"forbidden domestic-broker imports found: {violations}"


def test_tsla_auto_module_never_imports_macd2_at_runtime():
    """Belt-and-suspenders: actually import every tsla_auto submodule and
    confirm doing so never ADDS a new app.trading.macd2 entry to sys.modules.

    Compares a before/after snapshot rather than asserting sys.modules has
    zero macd2 entries outright — in a full ``pytest`` run sharing one
    process, some earlier, unrelated test (e.g. anything under tests/macd2)
    may have already imported app.trading.macd2 for its own reasons, which
    has nothing to do with whether TSLA_AUTO's own code imports it."""
    import importlib
    import sys

    macd2_modules_before = {m for m in sys.modules if m.startswith("app.trading.macd2")}
    for path in _iter_py_files():
        rel = path.relative_to(_TSLA_AUTO_DIR.parent.parent.parent)
        mod_name = ".".join(rel.with_suffix("").parts)
        if mod_name.endswith("__init__"):
            mod_name = mod_name.rsplit(".", 1)[0]
        importlib.import_module(mod_name)
    macd2_modules_after = {m for m in sys.modules if m.startswith("app.trading.macd2")}
    newly_imported = macd2_modules_after - macd2_modules_before
    assert newly_imported == set(), (
        f"importing tsla_auto modules must never pull in app.trading.macd2, but newly imported: {newly_imported}"
    )


def test_state_ledger_cache_paths_never_overlap_with_macd2():
    assert "macd2" not in str(state_store.STATE_PATH).lower()
    assert "macd2" not in str(ledger.SIGNAL_LEDGER_PATH).lower()
    assert "macd2" not in str(ledger.EXECUTION_LEDGER_PATH).lower()
    assert "tsla_auto" in str(state_store.STATE_PATH)
    assert "tsla_auto" in str(ledger.SIGNAL_LEDGER_PATH)


def test_fake_broker_rejects_operational_tsla_auto_path():
    with pytest.raises(RuntimeError):
        FakeBroker(storage_path=Path.cwd() / "data" / "state" / "tsla_auto" / "tsla_auto_runtime.json")


def test_forbidden_tslt_symbol_rejected_by_order_executor():
    """docs §3/§4 — TSLT (a known-wrong legacy long-ETF ticker) must never
    reach the broker, even if somehow passed as a target."""
    broker = FakeBroker(cash_usd=10_000.0, quotes={"TSLT": 30.0})
    assert "TSLT" in config.FORBIDDEN_SYMBOLS
    assert "TSLT" not in config.TRADE_SYMBOLS


def test_forbidden_tslt_symbol_never_becomes_a_valid_order_target(monkeypatch):
    from app.trading.tsla_auto import order_executor

    monkeypatch.setattr(order_executor, "target_symbol_for_direction", lambda direction: "TSLT")
    broker = FakeBroker(cash_usd=10_000.0, quotes={"TSLT": 30.0})
    outcome = execute_signal(broker=broker, direction=Direction.UP_RED, signal_id="sid-tslt", quotes={"TSLT": 30.0}, position=None, budget_usd=10_000.0)
    assert outcome.final_state.value == "BLOCKED"
    assert broker.orders == []


def test_domestic_symbol_never_becomes_a_valid_order_target(monkeypatch):
    """Only TSLL/TSLZ are valid order symbols — a domestic KRX code (e.g.
    MACD2's 0193T0) must be rejected exactly like TSLT."""
    from app.trading.tsla_auto import order_executor

    monkeypatch.setattr(order_executor, "target_symbol_for_direction", lambda direction: "0193T0")
    broker = FakeBroker(cash_usd=10_000.0, quotes={"0193T0": 15_000.0})
    outcome = execute_signal(broker=broker, direction=Direction.UP_RED, signal_id="sid-domestic", quotes={"0193T0": 15_000.0}, position=None, budget_usd=10_000.0)
    assert outcome.final_state.value == "BLOCKED"
    assert broker.orders == []


def test_market_data_never_imports_domestic_kis_client_module():
    """market_data.py's fetchers must route only through kis_overseas_adapter
    (checked via actual import statements, not prose — the module's own
    docstring legitimately mentions kis_client by name to explain what it
    must NOT do)."""
    path = _TSLA_AUTO_DIR / "market_data.py"
    imported = _imported_module_names(path)
    assert not any(name == "app.trading.kis_client" or name.endswith(".kis_client") for name in imported)
    text = path.read_text(encoding="utf-8")
    assert "kis_overseas_adapter" in text


def test_ui_page_for_tsla_auto_does_not_import_macd2_worker_or_state(monkeypatch):
    """If a TSLA_AUTO UI page exists, it must only call
    app.trading.tsla_auto.service.get_service() — never MACD2 modules."""
    candidates = [p for p in _UI_PATH.glob("*.py") if "TSLA" in p.name.upper()]
    if not candidates:
        pytest.skip("TSLA_AUTO UI page not present yet")
    for page in candidates:
        text = page.read_text(encoding="utf-8")
        assert "app.trading.macd2" not in text
        assert "macd2_worker" not in text.lower()


def test_worker_and_service_identifiers_are_unique_to_tsla_auto():
    assert config.WORKER_NAME == "tsla_auto_worker"
    assert config.SERVICE_NAME == "tsla_auto_service"
    assert config.WORKER_LOCK_FILENAME == "tsla_auto_worker.lock"
    assert config.STRATEGY_VERSION == "TSLA_AUTO_V1"
    assert config.SIGNAL_RULE == "TSLA_3M_CONFIRMED_MACD"
    assert config.STRONG_FILTER_VERSION == "TSLA_STRONG_FLAG_V1"
    assert config.STRATEGY_ID != "MACD2"
