"""Shared safety fixtures for tests/mu_macd — same discipline as
tests/macd2/conftest.py: no real KIS network, no writes under the real
data/ tree, isolated state/ledger per test, own service singleton reset.
"""
from __future__ import annotations

import socket

import pytest

from app.trading.mu_macd import ledger, service, state_store


@pytest.fixture(autouse=True)
def _fresh_mu_macd_service_singleton(monkeypatch):
    monkeypatch.setattr(service, "_service_singleton", None)


@pytest.fixture(autouse=True)
def _isolate_mu_macd_state(tmp_path, monkeypatch):
    """Force MU_MACD's own state store + ledgers onto tmp_path — never the
    real data/state/ or data/logs/ path, and never macd2's paths either."""
    monkeypatch.setattr(state_store, "STATE_DIR_PATH", tmp_path)
    monkeypatch.setattr(state_store, "STATE_PATH", tmp_path / "mu_macd_runtime.json")
    monkeypatch.setattr(ledger, "LOGS_DIR_PATH", tmp_path)
    monkeypatch.setattr(ledger, "SIGNAL_LEDGER_PATH", tmp_path / "mu_macd_signal_ledger.csv")
    monkeypatch.setattr(ledger, "EXECUTION_LEDGER_PATH", tmp_path / "mu_macd_execution_ledger.csv")
    yield


@pytest.fixture(autouse=True)
def _block_real_network(monkeypatch):
    def _blocked(*_args, **_kwargs):
        raise RuntimeError(
            "tests/mu_macd: real network access attempted — use MUMarketDataService(mode='mock') "
            "and inject ticks via on_tick(), never a real WebSocket/KIS client."
        )

    monkeypatch.setattr(socket.socket, "connect", _blocked, raising=True)
    monkeypatch.setattr(socket, "create_connection", _blocked, raising=True)
