"""Unit tests for app.trading.macd2.market_data — fake fetchers only, never real KIS."""
from __future__ import annotations

import time
from datetime import datetime, timedelta

import pandas as pd
import pytest

from app.trading.macd2 import config
from app.trading.macd2.market_data import MarketDataService, _candles_to_df

KST = config.KST


def _fake_bars_df(start: datetime, n_minutes: int) -> pd.DataFrame:
    rows = []
    for i in range(n_minutes):
        dt = start + timedelta(minutes=i)
        rows.append({"datetime": dt, "open": 100.0, "high": 100.5, "low": 99.5, "close": 100.0, "volume": 10})
    return pd.DataFrame(rows)


def test_bootstrap_ok_when_prior_day_and_enough_bars_present():
    prior_day = datetime(2026, 1, 5, 9, 0, tzinfo=KST)
    today = datetime(2026, 1, 6, 9, 0, tzinfo=KST)
    combined = pd.concat([_fake_bars_df(prior_day, 200), _fake_bars_df(today, 150)], ignore_index=True)

    def fake_fetch(mode, symbol, count, hour1):
        del mode, symbol, count, hour1
        return combined, {}

    svc = MarketDataService(mode="mock", fetch_minute_candles=fake_fetch)
    result = svc.bootstrap(now=today + timedelta(minutes=150, seconds=5))

    assert result.ok is True
    assert result.reason is None
    assert result.prior_day_1m_bars == 200
    assert result.today_1m_bars == 150
    assert result.completed_3m_count >= config.WARMUP_3M_BARS_MIN


def test_bootstrap_fails_today_only_even_with_enough_bars():
    today = datetime(2026, 1, 6, 9, 0, tzinfo=KST)
    combined = _fake_bars_df(today, 320)  # plenty of bars, but all today

    def fake_fetch(mode, symbol, count, hour1):
        del mode, symbol, count, hour1
        return combined, {}

    svc = MarketDataService(mode="mock", fetch_minute_candles=fake_fetch)
    result = svc.bootstrap(now=today + timedelta(minutes=320, seconds=5))

    assert result.ok is False
    assert result.reason == "TODAY_ONLY_NO_PRIOR_DAY"


def test_bootstrap_fails_on_no_data():
    def fake_fetch(mode, symbol, count, hour1):
        del mode, symbol, count, hour1
        return pd.DataFrame(columns=["datetime", "open", "high", "low", "close", "volume"]), {}

    svc = MarketDataService(mode="mock", fetch_minute_candles=fake_fetch)
    result = svc.bootstrap(now=datetime(2026, 1, 6, 9, 30, tzinfo=KST))

    assert result.ok is False
    assert result.reason == "NO_1M_BARS"


def test_merge_incremental_does_not_refetch_full_history():
    call_counts = []
    prior_day = datetime(2026, 1, 5, 9, 0, tzinfo=KST)
    today = datetime(2026, 1, 6, 9, 0, tzinfo=KST)
    # Bootstrap page (large `count`) already includes prior-day bars, so bootstrap
    # stops after exactly one page fetch and never touches the "incremental" branch.
    bootstrap_frame = pd.concat([_fake_bars_df(prior_day, 200), _fake_bars_df(today, 150)], ignore_index=True)
    incremental_frame = _fake_bars_df(today + timedelta(minutes=150), 3)

    def fake_fetch(mode, symbol, count, hour1):
        del mode, symbol, hour1
        call_counts.append(count)
        return (bootstrap_frame, {}) if count > 10 else (incremental_frame, {})

    svc = MarketDataService(mode="mock", fetch_minute_candles=fake_fetch)
    svc.bootstrap(now=today + timedelta(minutes=150, seconds=5))
    before = svc.get_history_df()
    assert len(call_counts) == 1  # bootstrap needed exactly one page (prior day already included)

    merged = svc.merge_incremental_1m(now=today + timedelta(minutes=153, seconds=5))

    assert call_counts[-1] == 10  # incremental call requested a small page, not the full history again
    assert len(merged) == len(before) + 3
    assert merged["datetime"].is_monotonic_increasing
    assert merged["datetime"].duplicated().sum() == 0


def test_refresh_quotes_populates_all_three_symbols_with_age():
    def fake_quote(mode, symbol):
        del mode
        return {"000660": 150000.0, "0193T0": 15000.0, "0197X0": 10000.0}.get(symbol), None

    svc = MarketDataService(mode="mock", fetch_quote=fake_quote)
    svc.refresh_quotes()

    for symbol in (config.WATCH_SYMBOL, config.LONG_SYMBOL, config.INVERSE_SYMBOL):
        snap = svc.get_quote(symbol)
        assert snap is not None
        assert snap.price > 0
        assert snap.age_sec is not None and snap.age_sec >= 0
        assert snap.error is None


def test_get_quote_reports_error_without_raising():
    def fake_quote(mode, symbol):
        del mode, symbol
        return None, "RATE_LIMITED"

    svc = MarketDataService(mode="mock", fetch_quote=fake_quote)
    svc.refresh_quotes(symbols=(config.WATCH_SYMBOL,))
    snap = svc.get_quote(config.WATCH_SYMBOL)

    assert snap is not None
    assert snap.error == "RATE_LIMITED"
    assert snap.price == 0.0


def test_get_quote_missing_symbol_returns_none():
    svc = MarketDataService(mode="mock", fetch_quote=lambda mode, symbol: (100.0, None))
    assert svc.get_quote("9999999") is None


def test_quote_updater_lifecycle():
    calls = {"n": 0}

    def fake_quote(mode, symbol):
        del mode, symbol
        calls["n"] += 1
        return 100.0, None

    svc = MarketDataService(mode="mock", fetch_quote=fake_quote)
    assert svc.quote_updater_alive() is False

    svc.start_quote_updater(interval_sec=0.05)
    try:
        assert svc.quote_updater_alive() is True
        time.sleep(0.2)
        assert calls["n"] >= 2  # ticked more than once
    finally:
        svc.stop_quote_updater(join_timeout=2.0)

    assert svc.quote_updater_alive() is False


def test_history_updater_lifecycle():
    calls = {"n": 0}

    def fake_fetch(mode, symbol, count, hour1):
        del mode, symbol, count, hour1
        calls["n"] += 1
        return pd.DataFrame(columns=["datetime", "open", "high", "low", "close", "volume"]), {}

    svc = MarketDataService(mode="mock", fetch_minute_candles=fake_fetch)
    assert svc.history_updater_alive() is False

    svc.start_history_updater(interval_sec=0.05)
    try:
        assert svc.history_updater_alive() is True
        time.sleep(0.2)
        assert calls["n"] >= 2  # ticked more than once, all from the updater thread
    finally:
        svc.stop_history_updater(join_timeout=2.0)

    assert svc.history_updater_alive() is False


def test_default_kis_client_created_once_and_reused(monkeypatch):
    """docs: KIS client는 서비스 시작 시 1개 생성·재사용 — the real (non-fake)
    fetchers must call create_kis_client() at most once per service
    instance, regardless of how many bootstrap/incremental/quote calls
    happen afterward."""
    created = []

    class _FakeKisClient:
        def get_minute_candles(self, symbol, period_min, count, hour1):
            return []

        def get_current_price(self, symbol):
            return {"current_price": 100.0}

    def fake_create_kis_client(mode):
        created.append(mode)
        return _FakeKisClient()

    import app.trading.kis_client as kis_client_module

    monkeypatch.setattr(kis_client_module, "create_kis_client", fake_create_kis_client)

    svc = MarketDataService(mode="mock")  # no fetch_minute_candles/fetch_quote injected -> uses the real defaults
    svc.bootstrap(now=datetime(2026, 1, 6, 9, 30, tzinfo=KST))
    svc.merge_incremental_1m()
    svc.refresh_quotes()
    svc.refresh_quotes()

    assert len(created) == 1  # exactly one client created for this service instance, reused every call


def test_candles_to_df_skips_malformed_rows():
    candles = [
        {"date": "20260106", "time": "090000", "open": 1, "high": 1, "low": 1, "close": 100.0, "volume": 1},
        {"date": "bad", "time": "090100", "open": 1, "high": 1, "low": 1, "close": 101.0, "volume": 1},
        {"date": "20260106", "time": "0902", "open": 1, "high": 1, "low": 1, "close": 102.0, "volume": 1},
    ]
    df = _candles_to_df(candles)
    assert len(df) == 1
    assert df.iloc[0]["close"] == 100.0
