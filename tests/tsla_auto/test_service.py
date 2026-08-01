"""Unit tests for app.trading.tsla_auto.service — READ_ONLY/MOCK only, no REAL."""
from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd
import pytest

from app.trading.tsla_auto import config, service, state_store
from app.trading.tsla_auto.market_data import MarketDataService
from app.trading.tsla_auto.models import RuntimeStatus

ET = config.ET
_START = datetime(2026, 7, 24, 9, 30, tzinfo=ET)


def _patch_bootstrap_ok(monkeypatch):
    df_1m = pd.DataFrame([
        {"datetime": _START + timedelta(minutes=i), "open": 100.0, "high": 100.1, "low": 99.9, "close": 100.0, "volume": 10}
        for i in range(310)
    ])

    def _fake_bootstrap(self, now=None):
        self._df_1m = df_1m
        from app.trading.tsla_auto.market_data import BootstrapResult

        return BootstrapResult(True, None, 310, 0, 310, 100, 0.01)

    monkeypatch.setattr(MarketDataService, "bootstrap", _fake_bootstrap)
    monkeypatch.setattr(MarketDataService, "get_history_df", lambda self: df_1m)


def test_get_service_returns_singleton():
    svc1 = service.get_service()
    svc2 = service.get_service()
    assert svc1 is svc2


def test_read_only_start_never_constructs_a_broker(monkeypatch):
    _patch_bootstrap_ok(monkeypatch)
    svc = service.TslaAutoService()
    result = svc.start(mode="READ_ONLY", budget_usd=10_000.0)
    assert result["ok"] is True
    assert svc._broker is None
    snapshot = svc.get_snapshot()
    assert snapshot["state"].auto_trade_on is False


def test_read_only_never_writes_a_lock_file(monkeypatch):
    _patch_bootstrap_ok(monkeypatch)
    svc = service.TslaAutoService()
    svc.start(mode="READ_ONLY", budget_usd=10_000.0)
    assert not service.LOCK_PATH.exists()


def test_mock_start_creates_worker_and_lock_then_stop_releases_it(monkeypatch):
    _patch_bootstrap_ok(monkeypatch)
    svc = service.TslaAutoService()
    result = svc.start(mode="MOCK", budget_usd=10_000.0)
    assert result["ok"] is True
    assert svc._worker is not None
    assert svc._worker.is_alive() is True
    assert service.LOCK_PATH.exists()

    svc.stop()
    assert svc._worker.is_alive() is False
    assert not service.LOCK_PATH.exists()


def test_mock_start_keeps_worker_alive_when_bootstrap_is_still_warming_up(monkeypatch):
    df_1m = pd.DataFrame([
        {"datetime": _START + timedelta(minutes=i), "open": 100.0, "high": 100.1, "low": 99.9, "close": 100.0, "volume": 10}
        for i in range(20)
    ])

    def _fake_bootstrap(self, now=None):
        self._df_1m = df_1m
        from app.trading.tsla_auto.market_data import BootstrapResult

        return BootstrapResult(False, "WARMUP_1M_LT_300", 20, 0, 20, 6, 0.01)

    monkeypatch.setattr(MarketDataService, "bootstrap", _fake_bootstrap)
    monkeypatch.setattr(MarketDataService, "merge_incremental_1m", lambda self, now=None: df_1m)
    svc = service.TslaAutoService()

    result = svc.start(mode="MOCK", budget_usd=10_000.0)

    assert result["ok"] is True
    assert result["reason"] == "WARMUP_1M_LT_300"
    assert svc._worker is not None
    assert svc._worker.is_alive() is True
    state = state_store.load_state()
    assert state.auto_trade_on is True
    assert state.stopped is False
    assert state.ui_mode == RuntimeStatus.BOOTSTRAPPING
    assert service.LOCK_PATH.exists()
    svc.stop()


def test_set_strong_filter_enabled_records_command_and_state(monkeypatch):
    svc = service.TslaAutoService()
    result = svc.set_strong_filter_enabled(True, changed_by="test")
    assert result["ok"] is True
    state = state_store.load_state()
    assert state.strong_filter_enabled is True
    assert state.strong_filter_enabled_by == "test"
    assert service.COMMANDS_PATH.exists()
    lines = service.COMMANDS_PATH.read_text(encoding="utf-8").strip().splitlines()
    assert any("set_strong_filter_enabled" in line for line in lines)


def test_supervisor_status_reports_lock_path_and_worker_alive(monkeypatch):
    _patch_bootstrap_ok(monkeypatch)
    svc = service.TslaAutoService()
    svc.start(mode="MOCK", budget_usd=10_000.0)
    status = svc.supervisor_status()
    assert status["worker_alive"] is True
    assert status["lock_exists"] is True
    svc.stop()


def test_start_command_is_recorded_with_mode():
    svc = service.TslaAutoService()
    result = svc.start(mode="READ_ONLY", budget_usd=5_000.0)
    lines = service.COMMANDS_PATH.read_text(encoding="utf-8").strip().splitlines()
    assert any('"action": "start"' in line and '"mode": "READ_ONLY"' in line for line in lines)


def test_real_mode_never_started_in_this_work_item(monkeypatch):
    """docs §16: REAL 활성화 값/확인문구는 이번 작업 범위가 아니다 — starting
    with mode=REAL fails closed at broker-construction time (raises, never
    silently proceeds) — even stronger than a graceful {"ok": False}."""
    _patch_bootstrap_ok(monkeypatch)
    svc = service.TslaAutoService()
    with pytest.raises(PermissionError):
        svc.start(mode="REAL", budget_usd=10_000.0)
    assert svc._broker is None
    assert not service.LOCK_PATH.exists()
