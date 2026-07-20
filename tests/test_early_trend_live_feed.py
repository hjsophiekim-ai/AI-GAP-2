"""
test_early_trend_live_feed.py — Early Trend Detector의 5초 주기 실시간 가격
히스토리(진짜 5/10/20/30초 기울기) 단위테스트.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from app.trading import early_trend_live_feed as feed


def _seed_history(prices: list[float], now: datetime, step_seconds: float = 5.0) -> dict:
    history: dict = {}
    start = now - timedelta(seconds=step_seconds * (len(prices) - 1))
    for i, price in enumerate(prices):
        history = feed.record_price_sample(history, "X", price, start + timedelta(seconds=step_seconds * i))
    return history


def test_record_price_sample_trims_older_than_max_history():
    now = datetime(2026, 7, 20, 10, 0, 0)
    history = {}
    for i in range(30):
        history = feed.record_price_sample(history, "X", 1000.0 + i, now + timedelta(seconds=5 * i))
    last_now = now + timedelta(seconds=5 * 29)
    ages = [
        (last_now - datetime.fromisoformat(s["t"])).total_seconds() for s in history["X"]
    ]
    assert all(age <= feed.MAX_HISTORY_SECONDS for age in ages)


def test_record_price_sample_ignores_missing_price():
    history = feed.record_price_sample({}, "X", None, datetime(2026, 7, 20, 10, 0, 0))
    assert history == {}


def test_slope_pct_at_returns_none_with_insufficient_history():
    now = datetime(2026, 7, 20, 10, 0, 0)
    history = feed.record_price_sample({}, "X", 1000.0, now)
    assert feed.slope_pct_at(history, "X", now, 30.0) is None


def test_slope_pct_at_computes_positive_slope_for_uptrend():
    now = datetime(2026, 7, 20, 10, 0, 30)
    prices = [1000.0, 1000.5, 1001.2, 1002.0, 1003.0, 1004.5, 1006.0]
    history = _seed_history(prices, now)
    slope_30s = feed.slope_pct_at(history, "X", now, 30.0)
    assert slope_30s is not None and slope_30s > 0


def test_slope_pct_at_computes_negative_slope_for_downtrend():
    now = datetime(2026, 7, 20, 10, 0, 30)
    prices = [1006.0, 1004.5, 1003.0, 1002.0, 1001.2, 1000.5, 1000.0]
    history = _seed_history(prices, now)
    slope_30s = feed.slope_pct_at(history, "X", now, 30.0)
    assert slope_30s is not None and slope_30s < 0


def test_compute_live_direction_requires_all_available_windows_to_agree():
    now = datetime(2026, 7, 20, 10, 0, 30)
    # 지속적 상승 — 5/10/20/30초 전부 UP으로 일치해야 한다.
    prices = [1000.0, 1000.5, 1001.2, 1002.0, 1003.0, 1004.5, 1006.0]
    history = _seed_history(prices, now)
    result = feed.compute_live_direction(history, "X", now)
    assert result["direction"] == "UP"
    assert result["windows_available"] >= 2


def test_compute_live_direction_returns_none_when_windows_disagree():
    now = datetime(2026, 7, 20, 10, 0, 30)
    # 30초 전 큰 하락 이후 최근 10초는 반등 중 — 짧은 구간(5/10초)은 UP, 긴
    # 구간(20/30초)은 여전히 DOWN을 가리켜 방향이 갈린다.
    prices = [1010.0, 990.0, 980.0, 975.0, 970.0, 972.0, 976.0]
    history = _seed_history(prices, now)
    result = feed.compute_live_direction(history, "X", now)
    assert result["direction"] is None


def test_compute_live_direction_ignores_noise_below_threshold():
    now = datetime(2026, 7, 20, 10, 0, 30)
    # 아주 미세한 흔들림(임계 미만) — 방향을 확정하지 않는다.
    prices = [1000.0, 1000.001, 1000.002, 1000.001, 1000.003, 1000.002, 1000.001]
    history = _seed_history(prices, now)
    result = feed.compute_live_direction(history, "X", now)
    assert result["direction"] is None
