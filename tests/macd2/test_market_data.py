"""Unit tests for app.trading.macd2.market_data — fake fetchers only, never real KIS."""
from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta

import pandas as pd
import pytest

from app.trading.macd2 import config, market_data as market_data_module
from app.trading.macd2.market_data import (
    MarketDataService,
    _candles_to_df,
    _empty_1m_frame,
    _load_prior_day_1m_cache,
    _prior_weekday_candidates,
    filter_complete_3m_bars,
)
from app.trading.macd2.signal_engine import resample_completed_3m

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


def test_merge_incremental_1m_retries_a_transient_fetch_error():
    """2026-08-11 fix (real incident: a confirmed flag's 3m bin never
    became actionable -- no exit of an already-held position, no new
    entry -- because this call had zero retry on a transient KIS error).
    A single failed attempt (empty + error) must not be treated the same
    as a genuine no-data-yet response; it must retry and pick up the real
    new bar once the fetch succeeds."""
    prior_day = datetime(2026, 1, 5, 9, 0, tzinfo=KST)
    today = datetime(2026, 1, 6, 9, 0, tzinfo=KST)
    bootstrap_frame = pd.concat([_fake_bars_df(prior_day, 200), _fake_bars_df(today, 150)], ignore_index=True)
    incremental_frame = _fake_bars_df(today + timedelta(minutes=150), 3)
    attempt_count = {"n": 0}

    def fake_fetch(mode, symbol, count, hour1):
        del mode, symbol, hour1
        if count > 10:
            return bootstrap_frame, {}
        attempt_count["n"] += 1
        if attempt_count["n"] == 1:
            return _empty_1m_frame(), {"error": "KIS_500"}
        return incremental_frame, {}

    svc = MarketDataService(mode="mock", fetch_minute_candles=fake_fetch)
    svc.bootstrap(now=today + timedelta(minutes=150, seconds=5))
    before = svc.get_history_df()

    merged = svc.merge_incremental_1m(now=today + timedelta(minutes=153, seconds=5))

    assert attempt_count["n"] == 2  # first attempt errored, retried once, second succeeded
    assert len(merged) == len(before) + 3  # the real new bars were NOT lost


def test_merge_incremental_1m_no_error_empty_response_returns_immediately():
    """A legitimately empty response (no error -- nothing new yet) must
    NOT be retried; only a genuine fetch error triggers a retry."""
    prior_day = datetime(2026, 1, 5, 9, 0, tzinfo=KST)
    today = datetime(2026, 1, 6, 9, 0, tzinfo=KST)
    bootstrap_frame = pd.concat([_fake_bars_df(prior_day, 200), _fake_bars_df(today, 150)], ignore_index=True)
    attempt_count = {"n": 0}

    def fake_fetch(mode, symbol, count, hour1):
        del mode, symbol, hour1
        if count > 10:
            return bootstrap_frame, {}
        attempt_count["n"] += 1
        return _empty_1m_frame(), {}

    svc = MarketDataService(mode="mock", fetch_minute_candles=fake_fetch)
    svc.bootstrap(now=today + timedelta(minutes=150, seconds=5))
    before = svc.get_history_df()

    merged = svc.merge_incremental_1m(now=today + timedelta(minutes=153, seconds=5))

    assert attempt_count["n"] == 1  # no retry for a clean empty (no error) response
    assert len(merged) == len(before)


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


class TestQuoteHistoryLockIndependence:
    """2026-08-25 fix (real incident: order_block_reason stuck at
    HISTORY_GAP all morning; every TW/TW2 T+3 confirmation stuck at
    PENDING_CONFIRMATION forever). A single self._io_lock used to wrap
    BOTH refresh_quotes() and merge_incremental_1m() end-to-end, INCLUDING
    kis_client's own rate-limit retry backoff sleeps (up to ~40s under
    sustained KIS mock-mode rate limiting) -- a stuck quote fetch held
    that same lock and blocked history's merge_incremental_1m() (and vice
    versa) for the whole stuck duration. The two paths must now be able to
    make progress independently."""

    def test_slow_quote_fetch_does_not_block_history_merge(self):
        release_quote = threading.Event()

        def slow_quote(mode, symbol):
            del mode, symbol
            release_quote.wait(timeout=5.0)
            return 10000.0, None

        def fast_history(mode, symbol, count, hour1):
            del mode, symbol, count, hour1
            return _fake_bars_df(datetime(2026, 1, 6, 9, 0, tzinfo=KST), 1), {}

        svc = MarketDataService(mode="mock", fetch_quote=slow_quote, fetch_minute_candles=fast_history)

        quote_thread = threading.Thread(target=svc.refresh_quotes)
        quote_thread.start()
        try:
            t0 = time.monotonic()
            svc.merge_incremental_1m()
            elapsed = time.monotonic() - t0
            assert elapsed < 1.0, (
                f"merge_incremental_1m() took {elapsed:.2f}s -- it must not wait on a stuck quote fetch"
            )
        finally:
            release_quote.set()
            quote_thread.join(timeout=5.0)
        assert not quote_thread.is_alive()

    def test_slow_history_fetch_does_not_block_quote_refresh(self):
        release_history = threading.Event()

        def slow_history(mode, symbol, count, hour1):
            del mode, symbol, count, hour1
            release_history.wait(timeout=5.0)
            return _empty_1m_frame(), {}

        def fast_quote(mode, symbol):
            del mode
            return {"000660": 150000.0, "0193T0": 15000.0, "0197X0": 10000.0}.get(symbol), None

        svc = MarketDataService(mode="mock", fetch_minute_candles=slow_history, fetch_quote=fast_quote)

        history_thread = threading.Thread(target=svc.merge_incremental_1m)
        history_thread.start()
        try:
            t0 = time.monotonic()
            svc.refresh_quotes()
            elapsed = time.monotonic() - t0
            assert elapsed < 1.0, (
                f"refresh_quotes() took {elapsed:.2f}s -- it must not wait on a stuck history fetch"
            )
        finally:
            release_history.set()
            history_thread.join(timeout=5.0)
        assert not history_thread.is_alive()

    def test_quote_fetch_calls_still_serialize_within_refresh_quotes(self):
        """The lock split must not let refresh_quotes() itself fire
        concurrent/overlapping KIS calls for its own symbols -- only cross
        -path (quote vs history) blocking is removed, never intra-path
        serialization (still exactly one KIS call in flight per path at a
        time, same as before this fix)."""
        concurrent_count = {"active": 0, "max_seen": 0}
        guard = threading.Lock()

        def tracking_quote(mode, symbol):
            del mode, symbol
            with guard:
                concurrent_count["active"] += 1
                concurrent_count["max_seen"] = max(concurrent_count["max_seen"], concurrent_count["active"])
            time.sleep(0.05)
            with guard:
                concurrent_count["active"] -= 1
            return 10000.0, None

        svc = MarketDataService(mode="mock", fetch_quote=tracking_quote)
        svc.refresh_quotes()
        assert concurrent_count["max_seen"] == 1


def test_watch_quote_is_normalized_to_latest_1m_close_scale():
    start = datetime(2026, 7, 24, 14, 30, tzinfo=KST)
    history = pd.DataFrame([
        {"datetime": start, "open": 178800.0, "high": 178800.0, "low": 178800.0, "close": 178800.0, "volume": 1}
    ])
    svc = MarketDataService(
        mode="mock",
        fetch_minute_candles=lambda *a: (history, {}),
        fetch_quote=lambda mode, symbol: (1_788_000.0 if symbol == config.WATCH_SYMBOL else 10_000.0, None),
    )
    svc.bootstrap(now=start + timedelta(minutes=1))

    svc.refresh_quotes(symbols=(config.WATCH_SYMBOL,))
    snap = svc.get_quote(config.WATCH_SYMBOL)

    assert snap is not None
    assert snap.price == 178800.0
    assert svc.quote_normalization_diag()["reason"] == "QUOTE_10X_HISTORY_CLOSE"


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
    fetchers must call create_kis_client() at most once per (mode, purpose)
    per service instance, regardless of how many bootstrap/incremental/quote
    calls happen afterward.

    2026-07-27 fix: WATCH_SYMBOL(000660) prior-day warm-up now reads via a
    SEPARATE, dedicated read-only REAL client
    (``_get_watch_symbol_history_client``, MOCK's date-scoped warm-up
    endpoint proved unreliable) — so a "mock"-mode service now legitimately
    creates at most 2 clients total (one "mock" for orders/quotes/today's
    live paging, one "real" for prior-day warm-up), each created exactly
    once and reused, never re-created per call."""
    created = []

    class _FakeKisClient:
        def get_minute_candles(self, symbol, period_min, count, hour1, market_div="J"):
            return []

        def get_minute_candles_for_date(self, symbol, date, period_min, count, hour1, market_div="J"):
            return []

        def get_current_price(self, symbol, market_div="J"):
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

    assert created.count("mock") == 1  # exactly one mock client, reused every call
    assert created.count("real") <= 1  # at most one dedicated read-only warm-up client, reused


def test_default_fetchers_request_nxt_market_div(monkeypatch):
    """2026-08-20 NXT fix: WATCH_SYMBOL(000660)의 1분봉은 이제 J(정규장 단독)가
    아니라 NX(NXT 포함 통합 체결가)를 유일한 소스로 써야 한다 — KIS 실제 차트가
    쓰는 것과 같은 소스(사용자 조건 1: J와 병합하지 말고 NX 단일 기준)."""
    seen_market_divs = []

    class _FakeKisClient:
        def get_minute_candles(self, symbol, period_min, count, hour1, market_div="J"):
            seen_market_divs.append(("today", market_div))
            return []

        def get_minute_candles_for_date(self, symbol, date, period_min, count, hour1, market_div="J"):
            seen_market_divs.append(("prior_day", market_div))
            return []

        def get_current_price(self, symbol, market_div="J"):
            return {"current_price": 100.0}

    import app.trading.kis_client as kis_client_module

    monkeypatch.setattr(kis_client_module, "create_kis_client", lambda mode: _FakeKisClient())

    svc = MarketDataService(mode="mock")
    svc.bootstrap(now=datetime(2026, 1, 6, 9, 30, tzinfo=KST))

    assert seen_market_divs, "no fetch was made"
    assert all(div == config.NXT_MARKET_DIV_CODE for _kind, div in seen_market_divs)


def test_live_quote_requests_nxt_for_watch_symbol_only(monkeypatch):
    """2026-08-20 fix: 정규장 마감(15:30) 이후 대시보드 현재가가 그대로
    멈춰있던 문제 -- get_current_price도 WATCH_SYMBOL(000660)에 한해 NX로
    조회해야 한다(실측: 장외 시간 J=1,691,000 vs NX=1,692,000, 계속 갱신되는
    쪽은 NX). 실제로 매매되는 ETF(LONG_SYMBOL/INVERSE_SYMBOL)는 이번 변경
    범위 밖이라 그대로 J를 써야 한다."""
    seen_market_divs = {}

    class _FakeKisClient:
        def get_minute_candles(self, symbol, period_min, count, hour1, market_div="J"):
            return []

        def get_minute_candles_for_date(self, symbol, date, period_min, count, hour1, market_div="J"):
            return []

        def get_current_price(self, symbol, market_div="J"):
            seen_market_divs[symbol] = market_div
            return {"current_price": 100.0}

    import app.trading.kis_client as kis_client_module

    monkeypatch.setattr(kis_client_module, "create_kis_client", lambda mode: _FakeKisClient())

    svc = MarketDataService(mode="mock")
    svc.refresh_quotes()

    assert seen_market_divs[config.WATCH_SYMBOL] == config.NXT_MARKET_DIV_CODE
    assert seen_market_divs[config.LONG_SYMBOL] == "J"
    assert seen_market_divs[config.INVERSE_SYMBOL] == "J"


def test_default_fetch_quote_surfaces_real_kis_error_reason(monkeypatch):
    """2026-08-20 fix: get_current_price()가 rate-limit(EGW00201) 등으로
    current_price=0 + error 메시지를 반환해도, 이전 코드는 항상 error=None을
    반환해 QuoteSnapshot.error에 진짜 원인이 아니라 일반적인
    "QUOTE_FETCH_FAILED"만 남았다 -- 실제 원인(rate limit인지, 심볼 오류인지)을
    진단할 수 없게 만든 원인 중 하나였다."""
    class _FakeKisClient:
        def get_current_price(self, symbol, market_div="J"):
            return {"current_price": 0, "rt_cd": "1", "msg_cd": "EGW00201", "error": "초당 거래건수를 초과하였습니다."}

    import app.trading.kis_client as kis_client_module

    monkeypatch.setattr(kis_client_module, "create_kis_client", lambda mode: _FakeKisClient())

    svc = MarketDataService(mode="mock")
    svc.refresh_quotes(symbols=(config.WATCH_SYMBOL,))

    snap = svc.get_quote(config.WATCH_SYMBOL)
    assert snap.error == "초당 거래건수를 초과하였습니다."


def test_bootstrap_fails_when_today_page_budget_exhausted_without_natural_stop(monkeypatch):
    """조건 6 회귀 테스트: 오늘 페이징 walk가 KIS_MAX_PAGES를 전부 소진할
    때까지 계속 새 데이터가 들어왔다면(PAGE_NO_GROWTH/CURSOR_NOT_MOVING 같은
    자연스러운 종료 신호 없이), 실제로는 더 이전 데이터가 남아있을 수 있는데도
    이를 "성공"으로 보고하면 안 된다 — market_div="NX" 전환으로 하루 세션이
    길어져(08:00~20:00) 이 위험이 커졌다."""
    monkeypatch.setattr(market_data_module, "KIS_MAX_PAGES", 3)
    prior_day = datetime(2026, 1, 5, 9, 0, tzinfo=KST)
    today = datetime(2026, 1, 6, 9, 0, tzinfo=KST)
    prior_frame = _fake_bars_df(prior_day, 200)

    call_n = {"n": 0}

    def fake_fetch(mode, symbol, count, hour1):
        del mode, symbol, count
        # Every page (up to KIS_MAX_PAGES=3) keeps returning fresh, growing,
        # never-repeating data -- there is never a natural PAGE_NO_GROWTH/
        # CURSOR_NOT_MOVING stop, so the walk only stops because it ran out
        # of page budget.
        call_n["n"] += 1
        anchor = today if not hour1 else datetime.strptime(hour1, "%H%M%S").replace(
            year=today.year, month=today.month, day=today.day, tzinfo=KST,
        )
        start = anchor - timedelta(minutes=30 * call_n["n"])
        return _fake_bars_df(start, 30), {}

    def fake_fetch_for_date(mode, symbol, date_ymd, count, hour1):
        del mode, symbol, date_ymd, count, hour1
        return prior_frame, {}

    svc = MarketDataService(
        mode="mock",
        fetch_minute_candles=fake_fetch,
        fetch_minute_candles_for_date=fake_fetch_for_date,
    )
    result = svc.bootstrap(now=today + timedelta(hours=1))

    assert result.ok is False
    assert result.reason == "NXT_TODAY_PAGE_BUDGET_EXHAUSTED"


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


def test_bootstrap_skips_past_persistent_page_error_instead_of_truncating():
    """2026-08-10 fix (real incident: 000660's minute-chart endpoint
    intermittently 500s at one specific hour1 boundary while other symbols'
    requests succeed) -- a page whose retries are ALL exhausted on a genuine
    fetch error must back off past that one stuck boundary and keep walking,
    not silently give up and amputate every earlier bar of the session."""
    today_open = datetime(2026, 1, 6, 9, 0, tzinfo=KST)
    recent_chunk = _fake_bars_df(today_open + timedelta(minutes=30), 30)  # 09:30-09:59
    early_chunk = _fake_bars_df(today_open, 30)  # 09:00-09:29

    def fake_fetch(mode, symbol, count, hour1):
        del mode, symbol, count
        if hour1 == "":
            return recent_chunk.copy(), {}
        if hour1 == "092900":  # boundary right before the earlier chunk -- always errors
            return _empty_1m_frame(), {"error": "KIS_500"}
        if hour1 == "085900":  # one page-width back-off past the stuck boundary
            return early_chunk.copy(), {}
        return _empty_1m_frame(), {}  # legitimate end of data

    svc = MarketDataService(mode="mock", fetch_minute_candles=fake_fetch)
    result = svc.bootstrap(now=today_open + timedelta(minutes=65))

    assert result.today_1m_bars == 60  # both chunks recovered, none amputated
    diag = svc.get_last_bootstrap_diag()
    stop_reasons = [p.get("stop_reason") for p in diag["kis_pages"]]
    assert "FETCH_ERROR_SKIPPED" in stop_reasons
    history = svc.get_history_df()
    assert (history["datetime"] == today_open).any()  # 09:00 bar survived


def test_bootstrap_gives_up_after_max_consecutive_page_error_skips():
    """A persistently, fully-down endpoint (every page errors, not just one
    stuck boundary) must still fail fast -- capped at
    MAX_CONSECUTIVE_PAGE_ERROR_SKIPS -- rather than burning the whole
    KIS_MAX_PAGES budget in retries."""
    call_count = {"n": 0}

    def fake_fetch_always_error(mode, symbol, count, hour1):
        del mode, symbol, count, hour1
        call_count["n"] += 1
        return _empty_1m_frame(), {"error": "KIS_500"}

    svc = MarketDataService(mode="mock", fetch_minute_candles=fake_fetch_always_error)
    result = svc.bootstrap(now=datetime(2026, 1, 6, 9, 10, tzinfo=KST))

    assert result.today_1m_bars == 0
    diag = svc.get_last_bootstrap_diag()
    # 1 initial page + MAX_CONSECUTIVE_PAGE_ERROR_SKIPS more before giving up
    assert len(diag["kis_pages"]) == 1 + config.MAX_CONSECUTIVE_PAGE_ERROR_SKIPS
    # each attempt retried PRIOR_DAY_FETCH_RETRIES times
    assert call_count["n"] == (1 + config.MAX_CONSECUTIVE_PAGE_ERROR_SKIPS) * config.PRIOR_DAY_FETCH_RETRIES


def test_bootstrap_warns_but_runs_when_today_history_starts_after_open():
    prior_day = datetime(2026, 1, 5, 9, 0, tzinfo=KST)
    late_today = datetime(2026, 1, 6, 11, 39, tzinfo=KST)
    combined = pd.concat([_fake_bars_df(prior_day, 381), _fake_bars_df(late_today, 90)], ignore_index=True)

    def fake_fetch(mode, symbol, count, hour1):
        del mode, symbol, count, hour1
        return combined.copy(), {}

    svc = MarketDataService(mode="mock", fetch_minute_candles=fake_fetch)
    result = svc.bootstrap(now=datetime(2026, 1, 6, 13, 9, tzinfo=KST))

    assert result.ok is True
    assert result.reason is None
    assert result.today_1m_bars == 90
    assert svc.get_last_bootstrap_diag()["today_history_warning"] == "TODAY_1M_START_AFTER_OPEN:11:39:00"


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


def test_filter_complete_3m_bars_drops_bin_missing_a_1m_bar():
    """docs §4: a 3-min bin only ever counts as confirmed when ALL 3 of its
    constituent 1-minute bars are present — never a silent partial bar."""
    start = datetime(2026, 7, 24, 9, 0, tzinfo=KST)
    rows = []
    for i in range(30):
        if i == 13:  # drop the middle minute of the 09:12-09:15 bin
            continue
        dt = start + timedelta(minutes=i)
        rows.append({"datetime": dt, "open": 100.0, "high": 100.1, "low": 99.9, "close": 100.0, "volume": 10})
    df_1m = pd.DataFrame(rows)
    now = start + timedelta(minutes=30)

    bars_3m = resample_completed_3m(df_1m, now=now)
    gapped_bar_start = start + timedelta(minutes=12)
    assert (bars_3m["datetime"] == gapped_bar_start).any()  # present before filtering (partial agg)

    filtered, dropped = filter_complete_3m_bars(bars_3m, df_1m)

    assert dropped == [pd.Timestamp(gapped_bar_start)]
    assert not (filtered["datetime"] == gapped_bar_start).any()
    assert len(filtered) == len(bars_3m) - 1


def test_filter_complete_3m_bars_keeps_all_bars_when_no_gap():
    start = datetime(2026, 7, 24, 9, 0, tzinfo=KST)
    df_1m = _fake_bars_df(start, 30)
    now = start + timedelta(minutes=30)

    bars_3m = resample_completed_3m(df_1m, now=now)
    filtered, dropped = filter_complete_3m_bars(bars_3m, df_1m)

    assert dropped == []
    assert len(filtered) == len(bars_3m)


def test_filter_complete_3m_bars_empty_1m_history_drops_everything():
    start = datetime(2026, 7, 24, 9, 0, tzinfo=KST)
    df_1m = _fake_bars_df(start, 30)
    now = start + timedelta(minutes=30)
    bars_3m = resample_completed_3m(df_1m, now=now)

    filtered, dropped = filter_complete_3m_bars(bars_3m, pd.DataFrame(columns=["datetime", "close"]))

    assert filtered.empty
    assert len(dropped) == len(bars_3m)
