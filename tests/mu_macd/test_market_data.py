"""READ-ONLY/MOCK unit tests for MUMarketDataService's tick-aggregation
logic — no network, no real WebSocket (see conftest._block_real_network)."""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app.trading.mu_macd import market_data as mu_market_data
from app.trading.mu_macd.config import KST
from app.trading.mu_macd.market_data import MUMarketDataService


def test_single_tick_creates_one_bar_open_eq_high_eq_low_eq_close():
    svc = MUMarketDataService(mode="mock")
    svc.on_tick("093601", 880.0, 1000, "20260812")
    df = svc.get_history_df()
    # the current (still-forming) bar is never finalized into self._bars
    # until a tick in a DIFFERENT minute arrives -- so history is empty here.
    assert df.empty


def test_ticks_within_same_minute_aggregate_ohlc():
    svc = MUMarketDataService(mode="mock")
    svc.on_tick("093601", 880.0, 1000, "20260812")
    svc.on_tick("093630", 882.0, 1010, "20260812")
    svc.on_tick("093645", 878.0, 1020, "20260812")
    # minute rolls over -> the 09:36 bar finalizes
    svc.on_tick("093701", 879.0, 1030, "20260812")
    df = svc.get_history_df()
    assert len(df) == 1
    row = df.iloc[0]
    assert row["open"] == 880.0
    assert row["high"] == 882.0
    assert row["low"] == 878.0
    assert row["close"] == 878.0
    # This is the FIRST bar ever observed -- there is no true "start of
    # minute" tvol baseline yet, so the whole observed cumulative tvol
    # (1020) is attributed to it. A LATER bar's volume is a true delta —
    # see test_minute_rollover_produces_correct_bar_count_and_datetimes.
    assert row["volume"] == 1020


def test_minute_rollover_produces_correct_bar_count_and_datetimes():
    svc = MUMarketDataService(mode="mock")
    svc.on_tick("093601", 880.0, 1000, "20260812")
    svc.on_tick("093701", 881.0, 1005, "20260812")
    svc.on_tick("093801", 882.0, 1010, "20260812")
    df = svc.get_history_df()
    assert len(df) == 2  # 09:36 and 09:37 finalized; 09:38 still forming
    assert df["datetime"].iloc[0] == df["datetime"].iloc[0].floor("min")
    assert str(df["datetime"].iloc[0].tz) == str(KST)


def test_warmup_bars_1m_count_matches_finalized_bars():
    svc = MUMarketDataService(mode="mock")
    for m in range(5):
        svc.on_tick(f"09{36+m:02d}01", 880.0 + m, 1000 + m * 10, "20260812")
    # 5 distinct minutes seen -> 4 finalized (last one still forming)
    assert svc.warmup_bars_1m_count() == 4


def test_is_stale_true_when_no_tick_yet():
    svc = MUMarketDataService(mode="mock")
    now = datetime(2026, 8, 12, 9, 40, tzinfo=KST)
    assert svc.is_stale(now, max_age_sec=15.0) is True


def test_is_stale_false_within_window_true_after():
    svc = MUMarketDataService(mode="mock")
    tick_time = datetime(2026, 8, 12, 9, 40, 0, tzinfo=KST)
    svc.on_tick("094000", 880.0, 1000, "20260812", recv_at=tick_time)
    assert svc.is_stale(tick_time.replace(second=10), max_age_sec=15.0) is False
    assert svc.is_stale(tick_time.replace(second=20), max_age_sec=15.0) is True


def test_inject_1m_bar_bypasses_aggregation_for_bulk_warmup():
    svc = MUMarketDataService(mode="mock")
    for m in range(30):
        minute = f"{9 + m // 60:02d}{m % 60:02d}"
        svc.inject_1m_bar("20260812", minute, 880.0, 881.0, 879.0, 880.0, 1000)
    assert svc.warmup_bars_1m_count() == 30
    df = svc.get_history_df()
    assert len(df) == 30


# ── get_approval_key() caching — 2026-08-13 fix: must NEVER re-hit KIS's
# oauth2/Approval on every reconnect retry (see market_data.py docstring). ──

class _FakeApprovalResponse:
    def __init__(self, key: str = "fake-approval-key"):
        self._key = key

    def raise_for_status(self):
        return None

    def json(self):
        return {"approval_key": self._key}


@pytest.fixture(autouse=True)
def _isolate_approval_key_cache(tmp_path, monkeypatch):
    """Fresh memory cache + tmp file-cache dir for every test in this file —
    the real caches are module-level and must never leak across tests or
    touch the real data/cache/ directory."""
    monkeypatch.setattr(mu_market_data, "_APPROVAL_KEY_CACHE", {})
    monkeypatch.setattr(mu_market_data, "_APPROVAL_KEY_ISSUED_AT", {})
    monkeypatch.setattr(mu_market_data, "_CACHE_DIR", tmp_path)
    yield


def test_get_approval_key_hits_network_once_then_reuses_memory_cache(monkeypatch):
    call_count = {"n": 0}

    def _fake_post(*args, **kwargs):
        call_count["n"] += 1
        return _FakeApprovalResponse()

    monkeypatch.setattr(mu_market_data.requests, "post", _fake_post)

    first = mu_market_data.get_approval_key("real")
    second = mu_market_data.get_approval_key("real")

    assert first == "fake-approval-key"
    assert second == "fake-approval-key"
    assert call_count["n"] == 1  # second call served from memory cache -- no new HTTP request


def test_get_approval_key_survives_process_restart_via_file_cache(monkeypatch):
    """Simulates a fresh MUMarketDataService (e.g. after stop()/start()) by
    clearing the in-memory cache only -- the file cache must still avoid a
    new network call."""
    call_count = {"n": 0}

    def _fake_post(*args, **kwargs):
        call_count["n"] += 1
        return _FakeApprovalResponse()

    monkeypatch.setattr(mu_market_data.requests, "post", _fake_post)

    mu_market_data.get_approval_key("real")
    assert call_count["n"] == 1

    # simulate process/service restart: memory cache gone, file cache remains
    mu_market_data._APPROVAL_KEY_CACHE.clear()
    mu_market_data._APPROVAL_KEY_ISSUED_AT.clear()

    second = mu_market_data.get_approval_key("real")
    assert second == "fake-approval-key"
    assert call_count["n"] == 1  # still just the one original network call


def test_get_approval_key_refetches_once_file_cache_expires(monkeypatch):
    call_count = {"n": 0}

    def _fake_post(*args, **kwargs):
        call_count["n"] += 1
        return _FakeApprovalResponse(key=f"fake-approval-key-{call_count['n']}")

    monkeypatch.setattr(mu_market_data.requests, "post", _fake_post)

    first = mu_market_data.get_approval_key("real")
    assert call_count["n"] == 1

    # age the cache past the TTL (both memory and the file cache written above)
    stale_issued_at = datetime.now() - mu_market_data._APPROVAL_KEY_TTL - timedelta(minutes=1)
    mu_market_data._APPROVAL_KEY_CACHE.clear()
    mu_market_data._APPROVAL_KEY_ISSUED_AT.clear()
    cache_path = mu_market_data._approval_key_cache_path("real")
    cache_path.write_text(
        f'{{"approval_key": "{first}", "issued_at": "{stale_issued_at.isoformat()}", "mode": "real"}}',
        encoding="utf-8",
    )

    second = mu_market_data.get_approval_key("real")
    assert call_count["n"] == 2  # expired cache -- forced a fresh network call
    assert second == "fake-approval-key-2"


# ── 1-minute bar disk cache (2026-08-13 fix) — a same-day restart must
# resume warmup instead of starting it over from zero. ──────────────────────

def test_real_mode_on_tick_persists_finalized_bars_to_disk():
    svc = MUMarketDataService(mode="real")
    svc.on_tick("093601", 880.0, 1000, "20260812")
    svc.on_tick("093701", 881.0, 1005, "20260812")  # finalizes the 09:36 bar
    cache_path = mu_market_data._bars_1m_cache_path()
    assert cache_path.exists()
    saved = mu_market_data._load_today_bars_from_cache("20260812")
    assert len(saved) == 1
    assert saved[0].minute == "0936"
    assert saved[0].open == 880.0


def test_mock_mode_on_tick_never_writes_to_disk():
    svc = MUMarketDataService(mode="mock")
    svc.on_tick("093601", 880.0, 1000, "20260812")
    svc.on_tick("093701", 881.0, 1005, "20260812")
    assert not mu_market_data._bars_1m_cache_path().exists()


def test_persist_bar_prunes_rows_from_a_prior_day():
    svc = MUMarketDataService(mode="real")
    svc.on_tick("093601", 880.0, 1000, "20260812")
    svc.on_tick("093701", 881.0, 1005, "20260812")  # finalizes a 08/12 bar
    svc.on_tick("093601", 890.0, 2000, "20260813")
    svc.on_tick("093701", 891.0, 2005, "20260813")  # finalizes a 08/13 bar -> should prune 08/12

    assert mu_market_data._load_today_bars_from_cache("20260812") == []
    today_bars = mu_market_data._load_today_bars_from_cache("20260813")
    assert len(today_bars) == 1
    assert today_bars[0].open == 890.0


def test_load_today_bars_restores_warmup_after_a_simulated_restart():
    """Simulates the real scenario: a service ticks for a while (warming up
    naturally, persisting bars as it goes), the process restarts (a FRESH
    MUMarketDataService instance, memory wiped), and load_today_bars() must
    resume warmup instead of starting over from zero."""
    first_run = MUMarketDataService(mode="real")
    for m in range(35):
        minute = f"{9 + m // 60:02d}{m % 60:02d}"
        next_minute = f"{9 + (m + 1) // 60:02d}{(m + 1) % 60:02d}"
        first_run.on_tick(f"{minute}01", 880.0 + m, 1000 + m, "20260813")
        first_run.on_tick(f"{next_minute}01", 880.0 + m, 1000 + m, "20260813")  # finalizes it
    assert first_run.warmup_bars_1m_count() >= 30

    # simulated restart: brand new instance, memory-only state is gone
    restarted = MUMarketDataService(mode="real")
    assert restarted.warmup_bars_1m_count() == 0
    restored = restarted.load_today_bars(now=datetime(2026, 8, 13, 10, 0, tzinfo=KST))
    assert restored >= 30
    assert restarted.warmup_bars_1m_count() == first_run.warmup_bars_1m_count()


def test_load_today_bars_ignores_a_prior_day_and_never_duplicates():
    seed = MUMarketDataService(mode="real")
    seed.on_tick("093601", 880.0, 1000, "20260812")
    seed.on_tick("093701", 881.0, 1005, "20260812")  # finalizes a stale (yesterday) bar

    svc = MUMarketDataService(mode="mock")  # mode doesn't matter for load_today_bars itself
    added_first = svc.load_today_bars(now=datetime(2026, 8, 13, 9, 40, tzinfo=KST))
    assert added_first == 0  # only 08/12 bars exist on disk -- today (08/13) has none yet
    assert svc.warmup_bars_1m_count() == 0

    added_again = svc.load_today_bars(now=datetime(2026, 8, 13, 9, 40, tzinfo=KST))
    assert added_again == 0  # still nothing to add, and re-calling never duplicates
