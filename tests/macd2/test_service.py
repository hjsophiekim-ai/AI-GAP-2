"""Unit tests for app.trading.macd2.service — FakeBroker + fake market data,
broker/market-data construction monkeypatched so start() never reaches the
real broker_factory/KIS client (conftest.py's network/KIS-client blocks would
fail the test immediately if it ever did)."""
from __future__ import annotations

import json
import math
import time as time_module
from datetime import datetime, timedelta

import pandas as pd
import pytest

from app.trading import strategy_ownership
from app.trading.macd2 import config, service as service_module, state_store
from app.trading.macd2.models import RuntimeStatus
from tests.macd2.fake_broker import FakeBroker

KST = config.KST


def _sine_1m_closes(n_minutes: int, amplitude: float = 20.0) -> list[float]:
    period = n_minutes
    return [round(100.0 + amplitude * math.sin(2 * math.pi * i / period), 4) for i in range(n_minutes)]


def _1m_frame(start: datetime, closes: list[float]) -> pd.DataFrame:
    rows = [
        {"datetime": start + timedelta(minutes=i), "open": c, "high": c + 0.1, "low": c - 0.1, "close": c, "volume": 10}
        for i, c in enumerate(closes)
    ]
    return pd.DataFrame(rows)


class _FakeMarketDataServiceOK:
    """Duck-types MarketDataService; bootstrap always succeeds."""

    def __init__(self, mode="mock"):
        self.mode = mode
        self._quote_updater_alive = False
        prior_day = datetime(2026, 1, 5, 9, 0, tzinfo=KST)
        self._df = _1m_frame(prior_day, _sine_1m_closes(300))

    def bootstrap(self, now=None):
        from app.trading.macd2.market_data import BootstrapResult
        return BootstrapResult(True, None, 300, 300, 0, 100, 0.01)

    def refresh_quotes(self, symbols=()):
        return {}

    def get_quote(self, symbol):
        return None

    def get_history_df(self):
        return self._df.copy()

    def merge_incremental_1m(self, now=None):
        return self._df.copy()

    def start_quote_updater(self, interval_sec=1.0):
        self._quote_updater_alive = True

    def stop_quote_updater(self, join_timeout=2.0):
        self._quote_updater_alive = False

    def quote_updater_alive(self):
        return self._quote_updater_alive

    def start_history_updater(self, interval_sec=5.0):
        self._history_updater_alive = True

    def stop_history_updater(self, join_timeout=2.0):
        self._history_updater_alive = False

    def history_updater_alive(self):
        return getattr(self, "_history_updater_alive", False)


class _FakeMarketDataServiceBootstrapFails(_FakeMarketDataServiceOK):
    def bootstrap(self, now=None):
        from app.trading.macd2.market_data import BootstrapResult
        return BootstrapResult(False, "TODAY_ONLY_NO_PRIOR_DAY", 300, 0, 300, 100, 0.01)


def _patch_ok_construction(monkeypatch, market_data_cls=_FakeMarketDataServiceOK):
    monkeypatch.setattr(service_module, "create_macd2_broker", lambda mode, **kw: FakeBroker(cash=10_000_000.0))
    monkeypatch.setattr(service_module, "MarketDataService", market_data_cls)


def test_start_blocks_when_enhanced_active(monkeypatch):
    monkeypatch.setattr(service_module, "other_strategy_active", lambda: (True, "ENHANCED_ACTIVE"))
    _patch_ok_construction(monkeypatch)

    svc = service_module.Macd2Service()
    res = svc.start(mode="mock", budget=1_000_000.0)

    assert res["ok"] is False
    assert res["message"] == "ENHANCED_ACTIVE"
    assert state_store.load_state().auto_trade_on is False


def test_start_blocks_when_macd_v1_active(monkeypatch, tmp_path):
    # Uses the real other_strategy_active() (not mocked), with Enhanced's own
    # check pinned to False so this genuinely isolates the MACD v1 file-read
    # branch (real-environment hynix_switch_state.load_state() defaults are
    # not something this test should depend on).
    import app.services.hynix_switch_state as enhanced_state

    monkeypatch.setattr(enhanced_state, "load_state", lambda *a, **k: {"auto_trade_on": False})

    v1_path = tmp_path / "macd_hynix_runtime.json"
    v1_path.write_text(json.dumps({"auto_trade_on": True}), encoding="utf-8")
    monkeypatch.setattr(strategy_ownership, "V1_RUNTIME_PATH", v1_path)
    _patch_ok_construction(monkeypatch)

    svc = service_module.Macd2Service()
    res = svc.start(mode="mock")

    assert res["ok"] is False
    assert res["message"] == "MACD_V1_ACTIVE"


def test_start_full_lifecycle_reaches_running(monkeypatch):
    monkeypatch.setattr(service_module, "other_strategy_active", lambda: (False, ""))
    _patch_ok_construction(monkeypatch)

    svc = service_module.Macd2Service()
    try:
        res = svc.start(mode="mock", budget=2_000_000.0)
        assert res["ok"] is True

        state = state_store.load_state()
        assert state.ui_mode == RuntimeStatus.RUNNING
        assert state.auto_trade_on is True
        assert state.warmup_ready is True
        assert state.budget == 2_000_000.0

        status = svc.supervisor_status()
        assert status["worker_alive"] is True
        assert status["quote_updater_alive"] is True
    finally:
        svc.stop()


def test_start_bootstrap_failure_never_starts_worker(monkeypatch):
    monkeypatch.setattr(service_module, "other_strategy_active", lambda: (False, ""))
    _patch_ok_construction(monkeypatch, market_data_cls=_FakeMarketDataServiceBootstrapFails)

    svc = service_module.Macd2Service()
    res = svc.start(mode="mock")

    assert res["ok"] is False
    assert "TODAY_ONLY_NO_PRIOR_DAY" in res["message"]
    state = state_store.load_state()
    assert state.ui_mode == RuntimeStatus.DATA_ERROR
    assert state.auto_trade_on is False
    assert svc.supervisor_status()["worker_alive"] is False


def test_start_twice_does_not_spawn_second_worker(monkeypatch):
    monkeypatch.setattr(service_module, "other_strategy_active", lambda: (False, ""))
    _patch_ok_construction(monkeypatch)

    svc = service_module.Macd2Service()
    try:
        svc.start(mode="mock")
        first_worker = svc._worker
        res2 = svc.start(mode="mock")
        assert res2 == {"ok": False, "message": "ALREADY_RUNNING"}
        assert svc._worker is first_worker
    finally:
        svc.stop()


def test_stop_sets_stopped_state_and_kills_worker(monkeypatch):
    monkeypatch.setattr(service_module, "other_strategy_active", lambda: (False, ""))
    _patch_ok_construction(monkeypatch)

    svc = service_module.Macd2Service()
    svc.start(mode="mock")
    assert svc.supervisor_status()["worker_alive"] is True

    res = svc.stop(reason="test_stop")
    assert res["ok"] is True
    assert svc.supervisor_status()["worker_alive"] is False
    assert svc.supervisor_status()["quote_updater_alive"] is False

    state = state_store.load_state()
    assert state.auto_trade_on is False
    assert state.stopped is True
    assert state.stopped_reason == "test_stop"
    assert state.ui_mode == RuntimeStatus.STOPPED


def test_get_snapshot_shape(monkeypatch):
    monkeypatch.setattr(service_module, "other_strategy_active", lambda: (False, ""))
    _patch_ok_construction(monkeypatch)

    svc = service_module.Macd2Service()
    try:
        svc.start(mode="mock")
        snap = svc.get_snapshot()
        assert "state" in snap and "worker" in snap and "quotes" in snap
        assert snap["worker"]["tick_n"] >= 0
    finally:
        svc.stop()


def test_get_service_returns_process_singleton(monkeypatch):
    monkeypatch.setattr(service_module, "other_strategy_active", lambda: (False, ""))
    a = service_module.get_service()
    b = service_module.get_service()
    assert a is b
