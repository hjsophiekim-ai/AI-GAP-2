"""Shared safety fixtures for tests/tsla_auto.

Every TSLA_AUTO test runs in isolation: no real KIS network calls, no writes
under the real data/ tree, and no dependency on MACD2/MACD v1/Enhanced state.
These fixtures are autouse for every test collected under this directory —
mirrors tests/macd2/conftest.py's pattern (re-implemented independently, docs
§3 완전 분리).
"""
from __future__ import annotations

import socket

import pytest

from app.trading.tsla_auto import config, ledger, market_data, service, state_store


@pytest.fixture(autouse=True)
def _fresh_service_singleton(monkeypatch):
    monkeypatch.setattr(service, "_service_instance", None)


@pytest.fixture(autouse=True)
def _isolate_tsla_auto_state(tmp_path, monkeypatch):
    """Force TSLA_AUTO's own state store + ledgers + cache + lock + command
    paths onto tmp_path — never the real data/state|ledger|cache|runtime|commands
    /tsla_auto/ paths."""
    monkeypatch.setattr(state_store, "STATE_DIR_PATH", tmp_path)
    monkeypatch.setattr(state_store, "STATE_PATH", tmp_path / "tsla_auto_runtime.json")
    monkeypatch.setattr(ledger, "LOGS_DIR_PATH", tmp_path)
    monkeypatch.setattr(ledger, "SIGNAL_LEDGER_PATH", tmp_path / "tsla_auto_signal_ledger.csv")
    monkeypatch.setattr(ledger, "EXECUTION_LEDGER_PATH", tmp_path / "tsla_auto_execution_ledger.csv")
    monkeypatch.setattr(market_data, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(service, "COMMANDS_DIR", tmp_path / "commands")
    monkeypatch.setattr(service, "COMMANDS_PATH", tmp_path / "commands" / "tsla_auto_commands.jsonl")
    monkeypatch.setattr(service, "LOCK_DIR", tmp_path / "runtime")
    monkeypatch.setattr(service, "LOCK_PATH", tmp_path / "runtime" / config.WORKER_LOCK_FILENAME)
    yield


@pytest.fixture(autouse=True)
def _block_real_network(monkeypatch):
    """Any attempt to open a real network socket fails the test immediately."""

    def _blocked(*_args, **_kwargs):
        raise RuntimeError(
            "tests/tsla_auto: real network access attempted — use a fake broker/market "
            "data provider instead (docs/TSLA_AUTO_LOGIC.md §완전 분리)."
        )

    monkeypatch.setattr(socket.socket, "connect", _blocked, raising=True)
    monkeypatch.setattr(socket, "create_connection", _blocked, raising=True)


@pytest.fixture(autouse=True)
def _block_real_kis_calls(monkeypatch):
    """TSLA_AUTO tests must use fake fetchers/FakeBroker — creating a real
    requests session against KIS is a test bug."""
    try:
        import requests
    except ImportError:
        return

    def _blocked(*_args, **_kwargs):
        raise RuntimeError(
            "tests/tsla_auto: requests.get/post called — TSLA_AUTO tests must inject "
            "a fake fetcher, never call the real KIS overseas endpoint."
        )

    monkeypatch.setattr(requests, "get", _blocked, raising=True)
    monkeypatch.setattr(requests, "post", _blocked, raising=True)
