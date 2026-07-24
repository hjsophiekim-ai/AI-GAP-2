"""Unit tests for app.trading.macd2.market_data — fake fetchers only, never real KIS."""
from __future__ import annotations

import time
from datetime import datetime, timedelta

import pandas as pd
import pytest

from app.trading.macd2 import config, market_data as market_data_module
from app.trading.macd2.market_data import (
    MarketDataService,
    _candles_to_df,
    _load_prior_day_1m_cache,
    _prior_weekday_candidates,
)

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
    assert result.reason == "TODAY_ONLY_WARMING_UP"


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


def test_bootstrap_falls_back_to_cache_when_kis_date_api_has_nothing(tmp_path, monkeypatch):
    """Fallback chain C-path (docs §21): fallback A (KIS 주식일별분봉조회)
    finds nothing for any candidate date (no fetch_minute_candles_for_date
    injected here — the autouse real-KIS-client block makes every fallback-A
    attempt fail), fallback B (persistent cache) has data -> bootstrap must
    still succeed using the cache, never requiring fallback A to work."""
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
    assert diag["prior_trading_day"]["source"] == "PERSISTENT_CACHE"
    assert diag["prior_trading_day"]["cache"]["prior_trading_date"] == "20260105"


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


def test_bootstrap_rejects_late_starting_today_history_after_open():
    prior_day = datetime(2026, 1, 5, 9, 0, tzinfo=KST)
    late_today = datetime(2026, 1, 6, 11, 39, tzinfo=KST)
    combined = pd.concat([_fake_bars_df(prior_day, 381), _fake_bars_df(late_today, 90)], ignore_index=True)

    def fake_fetch(mode, symbol, count, hour1):
        del mode, symbol, count, hour1
        return combined.copy(), {}

    svc = MarketDataService(mode="mock", fetch_minute_candles=fake_fetch)
    result = svc.bootstrap(now=datetime(2026, 1, 6, 13, 9, tzinfo=KST))

    assert result.ok is False
    assert result.reason == "TODAY_1M_START_AFTER_OPEN:11:39:00"
    assert result.today_1m_bars == 90


# ── Fallback A: KIS 주식일별분봉조회 (explicit trading-day search) ──────────

def test_prior_weekday_candidates_monday_finds_friday_first():
    """월요일에는 토/일을 건너뛰고 금요일이 첫 번째 후보여야 한다."""
    monday = "20260112"  # a real Monday
    candidates = _prior_weekday_candidates(monday, max_candidates=5)
    assert candidates[0] == "20260109"  # the preceding Friday
    assert "20260111" not in candidates  # Sunday
    assert "20260110" not in candidates  # Saturday


def test_prior_weekday_candidates_bounded_length():
    candidates = _prior_weekday_candidates("20260112", max_candidates=3)
    assert len(candidates) == 3


def test_bootstrap_holiday_then_next_day_finds_most_recent_real_trading_day():
    """공휴일 다음 날: 첫 후보(전날, 공휴일)는 빈 응답, 다음 후보(그 전
    거래일)에서 데이터를 찾아 그 날짜를 실제 전일 거래일로 선택해야 한다."""
    today = datetime(2026, 1, 6, 9, 0, tzinfo=KST)  # Tuesday
    holiday_ymd = "20260105"  # Monday — the first candidate, a holiday (empty)
    real_trading_ymd = "20260102"  # the preceding Friday — has real data

    def fake_fetch_for_date(mode, symbol, date_ymd, count, hour1):
        del mode, symbol, count, hour1
        if date_ymd == real_trading_ymd:
            day = datetime.strptime(date_ymd, "%Y%m%d").replace(tzinfo=KST)
            return _fake_bars_df(day, 380), {}
        return pd.DataFrame(columns=["datetime", "open", "high", "low", "close", "volume"]), {}

    def fake_fetch_today_empty(mode, symbol, count, hour1):
        del mode, symbol, count, hour1
        return pd.DataFrame(columns=["datetime", "open", "high", "low", "close", "volume"]), {}

    svc = MarketDataService(
        mode="mock", fetch_minute_candles=fake_fetch_today_empty,
        fetch_minute_candles_for_date=fake_fetch_for_date,
    )
    result = svc.bootstrap(now=today)

    assert result.ok is True
    assert result.prior_day_1m_bars == 380
    diag = svc.get_last_bootstrap_diag()
    assert diag["prior_trading_day"]["source"] == "KIS_DAILY_MINUTE_CHART"
    assert diag["prior_trading_day"]["selected_date"] == real_trading_ymd
    assert diag["prior_trading_day"]["candidates_tried"] == 2  # holiday date, then the real one


def test_bootstrap_warms_up_from_kis_date_api_alone_no_cache_needed(tmp_path, monkeypatch):
    """docs §21: a machine that has never run MACD2 before (no local cache
    at all) must still warm up successfully purely from fallback A."""
    monkeypatch.setattr(market_data_module, "CACHE_DIR", tmp_path)  # empty — no cache exists
    today = datetime(2026, 1, 6, 9, 0, tzinfo=KST)
    prior_day_ymd = "20260105"

    def fake_fetch_for_date(mode, symbol, date_ymd, count, hour1):
        del mode, symbol, count, hour1
        assert date_ymd == prior_day_ymd  # first weekday candidate, found immediately
        day = datetime.strptime(date_ymd, "%Y%m%d").replace(tzinfo=KST)
        return _fake_bars_df(day, 380), {}

    def fake_fetch_today(mode, symbol, count, hour1):
        del mode, symbol, count, hour1
        return pd.DataFrame(columns=["datetime", "open", "high", "low", "close", "volume"]), {}

    svc = MarketDataService(
        mode="mock", fetch_minute_candles=fake_fetch_today,
        fetch_minute_candles_for_date=fake_fetch_for_date,
    )
    result = svc.bootstrap(now=today)

    assert result.ok is True
    assert result.reason is None
    assert result.prior_day_1m_bars == 380
    diag = svc.get_last_bootstrap_diag()
    assert diag["prior_trading_day"]["source"] == "KIS_DAILY_MINUTE_CHART"
    assert diag["prior_trading_day"]["candidates_tried"] == 1  # succeeded on the very first candidate


def test_bootstrap_kis_date_api_fails_falls_back_to_cache(tmp_path, monkeypatch):
    """Fallback A explicitly fails/errors for every candidate date -> fallback
    B (persistent cache) must still deliver a successful warm-up."""
    monkeypatch.setattr(market_data_module, "CACHE_DIR", tmp_path)
    cache_dir = tmp_path / "naver_multi_1m"
    cache_dir.mkdir()
    rows = [f"2026-01-05 {9 + i // 60:02d}:{i % 60:02d}:00,100,100,100,100,10" for i in range(380)]
    (cache_dir / "000660_1m.csv").write_text(
        "datetime,open,high,low,close,volume\n" + "\n".join(rows) + "\n", encoding="utf-8",
    )

    def fake_fetch_for_date_always_fails(mode, symbol, date_ymd, count, hour1):
        del mode, symbol, date_ymd, count, hour1
        raise ConnectionError("KIS API unreachable")

    def fake_fetch_today(mode, symbol, count, hour1):
        del mode, symbol, count, hour1
        return pd.DataFrame(columns=["datetime", "open", "high", "low", "close", "volume"]), {}

    def _safe_fetch_for_date(mode, symbol, date_ymd, count, hour1):
        try:
            return fake_fetch_for_date_always_fails(mode, symbol, date_ymd, count, hour1)
        except ConnectionError as exc:
            return pd.DataFrame(columns=["datetime", "open", "high", "low", "close", "volume"]), {"error": repr(exc)}

    svc = MarketDataService(
        mode="mock", fetch_minute_candles=fake_fetch_today,
        fetch_minute_candles_for_date=_safe_fetch_for_date,
    )
    result = svc.bootstrap(now=datetime(2026, 1, 6, 8, 59, tzinfo=KST))

    assert result.ok is True
    assert result.prior_day_1m_bars == 380
    diag = svc.get_last_bootstrap_diag()
    assert diag["prior_trading_day"]["source"] == "PERSISTENT_CACHE"
    assert diag["prior_trading_day"]["candidates_tried"] == market_data_module.MAX_TRADING_DATE_LOOKBACK_DAYS


def test_bootstrap_all_sources_fail_reports_today_only_warming_up(tmp_path, monkeypatch):
    """Fallback A empty for every candidate AND fallback B (cache) empty ->
    TODAY_ONLY_WARMING_UP (not a hard error), search still bounded."""
    monkeypatch.setattr(market_data_module, "CACHE_DIR", tmp_path)  # no cache file at all
    attempts = {"n": 0}

    def fake_fetch_for_date_empty(mode, symbol, date_ymd, count, hour1):
        del mode, symbol, date_ymd, count, hour1
        attempts["n"] += 1
        return pd.DataFrame(columns=["datetime", "open", "high", "low", "close", "volume"]), {}

    def fake_fetch_today(mode, symbol, count, hour1):
        del mode, symbol, count, hour1
        return _fake_bars_df(datetime(2026, 1, 6, 9, 0, tzinfo=KST), 5), {}

    svc = MarketDataService(
        mode="mock", fetch_minute_candles=fake_fetch_today,
        fetch_minute_candles_for_date=fake_fetch_for_date_empty,
    )
    result = svc.bootstrap(now=datetime(2026, 1, 6, 9, 10, tzinfo=KST))

    assert result.ok is False
    assert result.reason == "TODAY_ONLY_WARMING_UP"
    assert attempts["n"] == market_data_module.MAX_TRADING_DATE_LOOKBACK_DAYS  # bounded — no infinite loop
    diag = svc.get_last_bootstrap_diag()
    assert diag["prior_trading_day"]["source"] == "NONE"


def test_candles_to_df_skips_malformed_rows():
    candles = [
        {"date": "20260106", "time": "090000", "open": 1, "high": 1, "low": 1, "close": 100.0, "volume": 1},
        {"date": "bad", "time": "090100", "open": 1, "high": 1, "low": 1, "close": 101.0, "volume": 1},
        {"date": "20260106", "time": "0902", "open": 1, "high": 1, "low": 1, "close": 102.0, "volume": 1},
    ]
    df = _candles_to_df(candles)
    assert len(df) == 1
    assert df.iloc[0]["close"] == 100.0
