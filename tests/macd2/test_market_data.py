"""Unit tests for app.trading.macd2.market_data — fake fetchers only, never real KIS."""
from __future__ import annotations

import time
from datetime import datetime, timedelta

import pandas as pd
import pytest

from app.trading.macd2 import config, market_data as market_data_module
from app.trading.macd2.market_data import MarketDataService, _candles_to_df, _load_prior_day_1m_cache

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
    # The live (large-count) fetch always returns the same combined frame
    # regardless of the hour1 cursor (this fake does not model a real KIS
    # cursor) — bootstrap's own no-growth check needs a second identical page
    # to detect that and stop, so exactly 2 large-page calls are expected,
    # never re-growing into a 3rd. The important behavior under test is the
    # one after bootstrap: merge_incremental_1m() must request a SMALL page
    # (count=10), never the large history page again.
    bootstrap_frame = pd.concat([_fake_bars_df(prior_day, 200), _fake_bars_df(today, 150)], ignore_index=True)
    incremental_frame = _fake_bars_df(today + timedelta(minutes=150), 3)

    def fake_fetch(mode, symbol, count, hour1):
        del mode, symbol, hour1
        call_counts.append(count)
        return (bootstrap_frame, {}) if count > 10 else (incremental_frame, {})

    svc = MarketDataService(mode="mock", fetch_minute_candles=fake_fetch)
    svc.bootstrap(now=today + timedelta(minutes=150, seconds=5))
    before = svc.get_history_df()
    assert call_counts == [120, 120]  # 1 real page + 1 to detect no further growth, then stop

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


def test_prior_day_cache_loads_most_recent_prior_trading_date(tmp_path, monkeypatch):
    """docs §21 (2026-07-24 bootstrap fix): KIS's live minute-candle endpoint
    has no date parameter and only ever returns TODAY — prior-day bars must
    come from this local cache instead, explicitly scoped to the most recent
    prior trading date found in the file (never today's own rows)."""
    monkeypatch.setattr(market_data_module, "CACHE_DIR", tmp_path)
    cache_dir = tmp_path / "naver_multi_1m"
    cache_dir.mkdir()
    rows = []
    for day, n in (("2026-01-05", 380), ("2026-01-06", 200)):  # two distinct prior dates
        for i in range(n):
            rows.append(f"{day} {9 + i // 60:02d}:{i % 60:02d}:00,100,100,100,100,10")
    (cache_dir / "000660_1m.csv").write_text(
        "datetime,open,high,low,close,volume\n" + "\n".join(rows) + "\n", encoding="utf-8",
    )

    df, diag = _load_prior_day_1m_cache("000660", today_ymd="20260107")

    assert diag["error"] is None
    assert diag["prior_trading_date"] == "20260106"  # the LATEST prior date, not the oldest
    assert len(df) == 200
    assert diag["received_count"] == 200
    assert list(df.columns) == ["datetime", "open", "high", "low", "close", "volume"]
    assert df["datetime"].iloc[0].tzinfo is not None  # tz-aware KST, matching the rest of macd2


def test_prior_day_cache_missing_file_reports_error_not_exception(tmp_path, monkeypatch):
    monkeypatch.setattr(market_data_module, "CACHE_DIR", tmp_path)
    df, diag = _load_prior_day_1m_cache("000660", today_ymd="20260107")
    assert df.empty
    assert diag["error"] == "NO_PRIOR_DAY_CACHE"


def test_bootstrap_uses_prior_day_cache_when_kis_only_has_today(tmp_path, monkeypatch):
    """The exact reported failure mode: KIS genuinely only returns today's
    bars (or none, pre-market) — bootstrap must still succeed using the
    prior-day cache, instead of reporting TODAY_ONLY_NO_PRIOR_DAY/NO_1M_BARS."""
    monkeypatch.setattr(market_data_module, "CACHE_DIR", tmp_path)
    cache_dir = tmp_path / "naver_multi_1m"
    cache_dir.mkdir()
    rows = [f"2026-01-05 {9 + i // 60:02d}:{i % 60:02d}:00,100,100,100,100,10" for i in range(380)]
    (cache_dir / "000660_1m.csv").write_text(
        "datetime,open,high,low,close,volume\n" + "\n".join(rows) + "\n", encoding="utf-8",
    )

    def fake_fetch_no_today_data(mode, symbol, count, hour1):
        del mode, symbol, count, hour1
        return pd.DataFrame(columns=["datetime", "open", "high", "low", "close", "volume"]), {}

    svc = MarketDataService(mode="mock", fetch_minute_candles=fake_fetch_no_today_data)
    result = svc.bootstrap(now=datetime(2026, 1, 6, 8, 59, tzinfo=KST))  # pre-market, no today bars yet

    assert result.ok is True
    assert result.reason is None
    assert result.prior_day_1m_bars == 380
    assert result.today_1m_bars == 0
    diag = svc.get_last_bootstrap_diag()
    assert diag["requested_trading_date"] == "20260106"
    assert diag["prior_day_cache"]["prior_trading_date"] == "20260105"


def test_bootstrap_kis_page_no_growth_stops_without_infinite_loop():
    """docs §21: KIS's today-only endpoint repeating the same page forever
    (identical hour1 cursor never surfacing new data) must not loop forever
    or beyond a bounded number of requests."""
    call_count = {"n": 0}
    same_page = _fake_bars_df(datetime(2026, 1, 6, 9, 0, tzinfo=KST), 5)

    def fake_fetch_repeating(mode, symbol, count, hour1):
        del mode, symbol, count, hour1
        call_count["n"] += 1
        return same_page.copy(), {}

    svc = MarketDataService(mode="mock", fetch_minute_candles=fake_fetch_repeating)
    result = svc.bootstrap(now=datetime(2026, 1, 6, 9, 10, tzinfo=KST))

    assert call_count["n"] <= market_data_module.KIS_MAX_PAGES
    assert call_count["n"] == 2  # 1st page + 1 to detect no growth, then stop
    assert result.today_1m_bars == 5
    diag = svc.get_last_bootstrap_diag()
    assert diag["kis_pages"][-1]["stop_reason"] == "PAGE_NO_GROWTH"


def test_candles_to_df_skips_malformed_rows():
    candles = [
        {"date": "20260106", "time": "090000", "open": 1, "high": 1, "low": 1, "close": 100.0, "volume": 1},
        {"date": "bad", "time": "090100", "open": 1, "high": 1, "low": 1, "close": 101.0, "volume": 1},
        {"date": "20260106", "time": "0902", "open": 1, "high": 1, "low": 1, "close": 102.0, "volume": 1},
    ]
    df = _candles_to_df(candles)
    assert len(df) == 1
    assert df.iloc[0]["close"] == 100.0
