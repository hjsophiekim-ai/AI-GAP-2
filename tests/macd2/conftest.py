"""Shared safety fixtures for tests/macd2.

Every MACD2 test runs in isolation: no real KIS network calls, no writes
under the real data/ tree, and no dependency on MACD v1 / Enhanced state
(docs/MACD2_LOGIC.md §18). These fixtures are autouse for every test
collected under this directory.
"""
from __future__ import annotations

import socket

import pytest

from app.trading.macd2 import ledger, market_data, service, state_store


@pytest.fixture(autouse=True)
def _fresh_service_singleton(monkeypatch):
    """Never let one test's Macd2Service process-level singleton leak into
    another test (across any file in tests/macd2)."""
    monkeypatch.setattr(service, "_service_instance", None)


@pytest.fixture(autouse=True)
def _isolate_macd2_state(tmp_path, monkeypatch):
    """Force MACD2's own state store + ledgers onto tmp_path — never the real
    data/state/ or data/logs/ path."""
    monkeypatch.setattr(state_store, "STATE_DIR_PATH", tmp_path)
    monkeypatch.setattr(state_store, "STATE_PATH", tmp_path / "macd2_runtime.json")
    monkeypatch.setattr(ledger, "LOGS_DIR_PATH", tmp_path)
    monkeypatch.setattr(ledger, "SIGNAL_LEDGER_PATH", tmp_path / "macd2_signal_ledger.csv")
    monkeypatch.setattr(ledger, "EXECUTION_LEDGER_PATH", tmp_path / "macd2_execution_ledger.csv")
    # bootstrap()'s prior-day cache loader (docs §21 2026-07-24 fix) reads
    # CACHE_DIR/naver_multi_1m/{symbol}_1m.csv directly — must never resolve
    # to the real data/cache/ tree in a test.
    monkeypatch.setattr(market_data, "CACHE_DIR", tmp_path / "cache")
    yield


@pytest.fixture(autouse=True)
def _block_real_network(monkeypatch):
    """Any attempt to open a real network socket fails the test immediately."""

    def _blocked(*_args, **_kwargs):
        raise RuntimeError(
            "tests/macd2: real network access attempted — use a fake broker/market "
            "data provider instead (docs/MACD2_LOGIC.md §18)."
        )

    monkeypatch.setattr(socket.socket, "connect", _blocked, raising=True)
    monkeypatch.setattr(socket, "create_connection", _blocked, raising=True)


@pytest.fixture(autouse=True)
def _block_real_kis_client(monkeypatch):
    """MACD2 tests must use a fake broker — creating a real KIS client is a test bug."""
    try:
        import app.trading.kis_client as kis_client_module
    except ImportError:
        return

    def _blocked(*_args, **_kwargs):
        raise RuntimeError(
            "tests/macd2: create_kis_client() called — MACD2 tests must use a fake "
            "broker/market data provider, never a real KIS client."
        )

    monkeypatch.setattr(kis_client_module, "create_kis_client", _blocked, raising=False)
