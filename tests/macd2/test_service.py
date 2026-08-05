"""Unit tests for app.trading.macd2.service — FakeBroker + fake market data,
broker/market-data construction monkeypatched so start() never reaches the
real broker_factory/KIS client (conftest.py's network/KIS-client blocks would
fail the test immediately if it ever did)."""
from __future__ import annotations

import math
import time as time_module
from datetime import datetime, timedelta

import pandas as pd
import pytest

from app.trading.macd2 import config, order_executor, service as service_module, state_store
from app.trading.macd2.models import Direction, PositionSnapshot, QuoteSnapshot, RuntimeStatus
from app.trading.macd2.worker import ORDER_FILL_RECONCILE_DELAY_SEC, ORDER_FILL_RECONCILE_RETRIES
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

    def get_last_bootstrap_diag(self):
        return {}


class _FakeMarketDataServiceBootstrapFails(_FakeMarketDataServiceOK):
    def bootstrap(self, now=None):
        from app.trading.macd2.market_data import BootstrapResult
        return BootstrapResult(False, "TODAY_ONLY_WARMING_UP", 300, 0, 300, 100, 0.01)


class _FakeMarketDataServiceBootstrapFailsThenSucceeds(_FakeMarketDataServiceOK):
    """First bootstrap() call fails; every call after that succeeds — for
    exercising Macd2Service.retry_bootstrap() without a new thread/instance."""

    def __init__(self, mode="mock"):
        super().__init__(mode)
        self._bootstrap_calls = 0

    def bootstrap(self, now=None):
        from app.trading.macd2.market_data import BootstrapResult
        self._bootstrap_calls += 1
        if self._bootstrap_calls == 1:
            return BootstrapResult(False, "TODAY_ONLY_WARMING_UP", 300, 0, 300, 100, 0.01)
        return BootstrapResult(True, None, 300, 300, 0, 100, 0.01)


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
        deadline = time_module.time() + 2.0
        while svc.supervisor_status()["tick_n"] < 1 and time_module.time() < deadline:
            time_module.sleep(0.05)

        status = svc.supervisor_status()
        assert status["worker_alive"] is True
        assert status["tick_n"] >= 1
        assert status["started_at"]
        assert status["last_tick_at"]
        state = state_store.load_state()
        assert state.worker_instance_id == status["instance_id"]
        assert state.session_started_at
        assert state.ui_mode == RuntimeStatus.RUNNING
    finally:
        svc.stop()


def test_start_bootstrap_failure_never_starts_worker(monkeypatch):
    monkeypatch.setattr(service_module, "other_strategy_active", lambda: (False, ""))
    _patch_ok_construction(monkeypatch, market_data_cls=_FakeMarketDataServiceBootstrapFails)

    svc = service_module.Macd2Service()
    res = svc.start(mode="mock")

    assert res["ok"] is False
    assert "TODAY_ONLY_WARMING_UP" in res["message"]
    state = state_store.load_state()
    assert state.ui_mode == RuntimeStatus.DATA_ERROR
    assert state.auto_trade_on is False
    assert svc.supervisor_status()["worker_alive"] is False


def test_start_bootstrap_failure_keeps_quote_updater_running(monkeypatch):
    """docs §21 (2026-07-24 bootstrap fix): quote lifecycle is independent of
    bootstrap — live prices must keep updating even when history bootstrap
    fails, so the UI is never blind just because warmup/orders are blocked."""
    monkeypatch.setattr(service_module, "other_strategy_active", lambda: (False, ""))
    _patch_ok_construction(monkeypatch, market_data_cls=_FakeMarketDataServiceBootstrapFails)

    svc = service_module.Macd2Service()
    res = svc.start(mode="mock")

    assert res["ok"] is False
    assert svc.supervisor_status()["quote_updater_alive"] is True
    assert svc.supervisor_status()["worker_alive"] is False


def test_retry_bootstrap_starts_worker_after_initial_failure(monkeypatch):
    """docs §21: manual bootstrap retry (재시도 버튼) reuses the existing
    broker/MarketDataService — no new thread, no new instance — and starts
    the Worker once a later attempt succeeds."""
    monkeypatch.setattr(service_module, "other_strategy_active", lambda: (False, ""))
    _patch_ok_construction(monkeypatch, market_data_cls=_FakeMarketDataServiceBootstrapFailsThenSucceeds)

    svc = service_module.Macd2Service()
    try:
        first = svc.start(mode="mock")
        assert first["ok"] is False
        assert svc.supervisor_status()["worker_alive"] is False
        market_data_before_retry = svc._market_data

        retry = svc.retry_bootstrap()
        assert retry["ok"] is True
        assert svc._market_data is market_data_before_retry  # same instance, no new thread/service
        assert svc.supervisor_status()["worker_alive"] is True

        state = state_store.load_state()
        assert state.ui_mode == RuntimeStatus.RUNNING
        assert state.auto_trade_on is True
        assert state.warmup_ready is True

        # Retrying again while already running is a safe no-op.
        again = svc.retry_bootstrap()
        assert again == {"ok": True, "message": "ALREADY_RUNNING"}
    finally:
        svc.stop()


def test_retry_bootstrap_before_start_is_rejected():
    svc = service_module.Macd2Service()
    res = svc.retry_bootstrap()
    assert res == {"ok": False, "message": "NOT_STARTED"}


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


class _FakeMarketDataServiceWithQuotes(_FakeMarketDataServiceOK):
    """Adds real get_quote() so manual_entry() passes its QUOTE_UNAVAILABLE check."""

    def get_quote(self, symbol):
        return QuoteSnapshot(
            symbol=symbol, price=100.0, fetched_at=datetime.now(KST), age_sec=0.1, source="fake",
        )


def _patch_ok_construction_with_broker_quotes(monkeypatch, market_data_cls=_FakeMarketDataServiceWithQuotes):
    quotes = {config.LONG_SYMBOL: 100.0, config.INVERSE_SYMBOL: 100.0}
    monkeypatch.setattr(
        service_module, "create_macd2_broker",
        lambda mode, **kw: FakeBroker(cash=10_000_000.0, quotes=quotes),
    )
    monkeypatch.setattr(service_module, "MarketDataService", market_data_cls)


def test_manual_entry_uses_worker_reconcile_window(monkeypatch):
    """2026-08-05 fix: manual_entry() must pass the same fill-confirmation
    poll window (ORDER_FILL_RECONCILE_RETRIES/DELAY_SEC, 60s @ 1s) as the
    auto-trade Worker path — not order_executor.execute_signal's short
    default (5 @ 0.5s = 2.5s). The short default let a slow KIS fill
    response (e.g. at market open) time out the confirmation poll, so the
    order was silently dropped from the 거래원장 even though it filled."""
    monkeypatch.setattr(service_module, "other_strategy_active", lambda: (False, ""))
    _patch_ok_construction_with_broker_quotes(monkeypatch)

    captured = {}
    real_execute_signal = order_executor.execute_signal

    def _spy_execute_signal(**kwargs):
        captured.update(kwargs)
        return real_execute_signal(**kwargs)

    monkeypatch.setattr(order_executor, "execute_signal", _spy_execute_signal)

    svc = service_module.Macd2Service()
    try:
        svc.start(mode="mock", budget=1_000_000.0)
        res = svc.manual_entry(Direction.DOWN_BLUE.value)
        assert res["ok"] is True
        assert captured["reconcile_retries"] == ORDER_FILL_RECONCILE_RETRIES
        assert captured["reconcile_delay_sec"] == ORDER_FILL_RECONCILE_DELAY_SEC
    finally:
        svc.stop()


def test_manual_exit_uses_worker_reconcile_window(monkeypatch):
    """Same fix as test_manual_entry_uses_worker_reconcile_window, for the
    수동 전량매도 button's execute_exit() call."""
    monkeypatch.setattr(service_module, "other_strategy_active", lambda: (False, ""))
    _patch_ok_construction_with_broker_quotes(monkeypatch)

    captured = {}
    real_execute_exit = order_executor.execute_exit

    def _spy_execute_exit(**kwargs):
        captured.update(kwargs)
        return real_execute_exit(**kwargs)

    monkeypatch.setattr(order_executor, "execute_exit", _spy_execute_exit)

    svc = service_module.Macd2Service()
    try:
        svc.start(mode="mock", budget=1_000_000.0)
        entry = svc.manual_entry(Direction.DOWN_BLUE.value)
        assert entry["ok"] is True

        res = svc.manual_exit()
        assert res["ok"] is True
        assert captured["reconcile_retries"] == ORDER_FILL_RECONCILE_RETRIES
        assert captured["reconcile_delay_sec"] == ORDER_FILL_RECONCILE_DELAY_SEC
    finally:
        svc.stop()


def test_stop_and_liquidate_all_sells_held_position(monkeypatch):
    """UI "자동매매 중지 및 일괄매도" 버튼: Worker를 멈추고, 그 시점에 실제
    브로커가 들고 있는 TRADE_SYMBOLS 포지션을 order_executor.execute_exit로
    시장가 매도한 뒤 auto_trade_on/position을 모두 정리한다."""
    monkeypatch.setattr(service_module, "other_strategy_active", lambda: (False, ""))
    _patch_ok_construction(monkeypatch)

    svc = service_module.Macd2Service()
    try:
        svc.start(mode="mock")
        assert svc.supervisor_status()["worker_alive"] is True

        broker = svc._broker
        broker.set_quote(config.LONG_SYMBOL, 15000.0)
        broker.buy_market(config.LONG_SYMBOL, 100, "test:BUY")
        state = state_store.load_state()
        state.position = PositionSnapshot(symbol=config.LONG_SYMBOL, quantity=100, avg_price=15000.0)
        state_store.save_state(state)

        res = svc.stop_and_liquidate_all("test_liquidate")

        assert res["ok"] is True
        assert res["results"] == [
            {"symbol": config.LONG_SYMBOL, "quantity": 100, "ok": True, "final_state": "EXECUTED", "block_reason": config.EXIT_USER_LIQUIDATION},
        ]
        assert broker.get_position(config.LONG_SYMBOL) is None
        assert svc.supervisor_status()["worker_alive"] is False

        state = state_store.load_state()
        assert state.auto_trade_on is False
        assert state.stopped is True
        assert state.stopped_reason == "test_liquidate"
        assert state.ui_mode == RuntimeStatus.STOPPED
        assert state.position is None
    finally:
        svc.stop()


def test_stop_and_liquidate_all_noop_when_flat(monkeypatch):
    monkeypatch.setattr(service_module, "other_strategy_active", lambda: (False, ""))
    _patch_ok_construction(monkeypatch)

    svc = service_module.Macd2Service()
    try:
        svc.start(mode="mock")
        res = svc.stop_and_liquidate_all("test_liquidate_flat")

        assert res == {"ok": True, "results": []}
        state = state_store.load_state()
        assert state.auto_trade_on is False
        assert state.stopped is True
    finally:
        svc.stop()


def test_stop_and_liquidate_all_before_start_is_rejected():
    svc = service_module.Macd2Service()
    res = svc.stop_and_liquidate_all()
    assert res == {"ok": False, "message": "NOT_STARTED", "results": []}


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
