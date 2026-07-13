"""
test_hynix_short_term_signal.py — 단기 전고점 예측 모듈 테스트.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd
import pytest

from app.models.hynix_short_term_signal import predict_hynix_signal


def _daily_df(n: int = 30, base: float = 200_000.0) -> pd.DataFrame:
    rows = []
    start = datetime.now() - timedelta(days=n)
    price = base
    for i in range(n):
        price = price * (1 + (0.01 if i % 3 == 0 else -0.005))
        rows.append({
            "datetime": start + timedelta(days=i),
            "open": price * 0.99,
            "high": price * 1.02,
            "low": price * 0.98,
            "close": price,
            "volume": 1_000_000 + i * 1000,
        })
    return pd.DataFrame(rows)


def _minute_df(n: int = 60, base: float = 200_000.0) -> pd.DataFrame:
    rows = []
    start = datetime.now() - timedelta(minutes=n)
    price = base
    for i in range(n):
        price = price * (1 + 0.0005)
        rows.append({
            "datetime": start + timedelta(minutes=i),
            "open": price * 0.999,
            "high": price * 1.001,
            "low": price * 0.998,
            "close": price,
            "volume": 10000 + i * 10,
        })
    return pd.DataFrame(rows)


def _mu_1min_df() -> pd.DataFrame:
    now = datetime.now()
    rows = [
        {"datetime": now - timedelta(minutes=120), "open": 118.0, "high": 119.0, "low": 117.5, "close": 118.5, "volume": 5000, "session": "regular"},
        {"datetime": now - timedelta(minutes=90), "open": 118.5, "high": 120.0, "low": 118.0, "close": 119.8, "volume": 5200, "session": "regular"},
        {"datetime": now - timedelta(minutes=60), "open": 119.8, "high": 120.5, "low": 119.5, "close": 120.2, "volume": 4800, "session": "aftermarket"},
        {"datetime": now - timedelta(minutes=30), "open": 120.2, "high": 120.8, "low": 119.9, "close": 120.5, "volume": 4600, "session": "aftermarket"},
    ]
    return pd.DataFrame(rows)


def _complete_market_data() -> dict:
    df_daily = _daily_df()
    df_1min = _minute_df()
    current_price = float(df_daily.iloc[-1]["close"])
    return {
        "mu": {"df_1min": _mu_1min_df(), "current_price": {"price": 120.5}},
        "nvda": {"regular_return": 1.5},
        "amd": {"regular_return": 1.2},
        "index": {"sox_return": -0.5, "qqq_return": 0.8, "usdkrw_change": 0.3},
        "domestic_index": {"kospi_return": 0.4, "kospi200_return": 0.5},
        "hynix": {
            "current_price": current_price,
            "df_daily": df_daily,
            "prev_close": float(df_daily.iloc[-2]["close"]),
            "current_price_sources": {"KIS": current_price, "naver": current_price, "yfinance": current_price},
        },
        "hynix_minute": {
            "df_1min": df_1min,
            "status": "success",
            "last_bar_time": df_1min.iloc[-1]["datetime"].isoformat(),
        },
        "investor_flow": {"foreign_net_buy": 50000, "institution_net_buy": -10000},
        "kospilab": {},
        "news": {"score": 6.0, "success": True},
    }


class TestPredictHynixSignalComplete:
    def test_score_in_range(self):
        result = predict_hynix_signal(_complete_market_data())
        assert not result["blocked"]
        assert 0.0 <= result["short_term_score"] <= 100.0

    def test_target_ordering(self):
        result = predict_hynix_signal(_complete_market_data())
        t1, t2, t3 = result["target_levels"]
        assert t3 >= t2 >= 0

    def test_probabilities_monotonic(self):
        result = predict_hynix_signal(_complete_market_data())
        p1 = result["target_probabilities"]["target_1"]
        p2 = result["target_probabilities"]["target_2"]
        p3 = result["target_probabilities"]["target_3"]
        assert p1 >= p2 >= p3
        for p in (p1, p2, p3):
            assert 0.0 <= p <= 100.0

    def test_disclaimer_present(self):
        result = predict_hynix_signal(_complete_market_data())
        assert "확률" in result["disclaimer"]

    def test_no_overconfident_language(self):
        result = predict_hynix_signal(_complete_market_data())
        for text in [result["judgement"]] + result["reasons_top5"]:
            assert "확정" not in text
            assert "무조건" not in text

    def test_news_failure_uses_neutral_and_warns(self):
        data = _complete_market_data()
        data["news"] = {"score": 5.0, "success": False}
        result = predict_hynix_signal(data)
        assert not result["blocked"]
        assert result["news_warning"] is not None
        assert result["score_breakdown"]["news_momentum"] == 5.0

    def test_missing_micron_real_data_does_not_block_uses_proxy(self):
        """마이크론 정규장/장외 실데이터가 없어도(장 마감 등) Micron Proxy로 대체하고
        전체 제안 생성을 차단하지 않는다."""
        data = _complete_market_data()
        data["mu"] = {"df_1min": None, "current_price": None}
        result = predict_hynix_signal(data)
        assert not result["blocked"]
        assert result["micron_regular_score_source"] != "REAL"
        assert 0.0 <= result["score_breakdown"]["micron_regular"] <= 20.0


class TestPredictHynixSignalBlocked:
    def test_missing_current_price_blocks(self):
        data = _complete_market_data()
        data["hynix"]["current_price"] = None
        result = predict_hynix_signal(data)
        assert result["blocked"] is True
        assert any("현재가" in m for m in result["missing_data"])

    def test_missing_minute_blocks(self):
        data = _complete_market_data()
        data["hynix_minute"]["df_1min"] = None
        result = predict_hynix_signal(data)
        assert result["blocked"] is True
        assert any("분봉" in m for m in result["missing_data"])

    def test_missing_investor_flow_blocks(self):
        data = _complete_market_data()
        data["investor_flow"] = {"foreign_net_buy": None, "institution_net_buy": None}
        result = predict_hynix_signal(data)
        assert result["blocked"] is True
        assert any("순매수" in m for m in result["missing_data"])

    def test_blocked_result_still_has_disclaimer(self):
        data = _complete_market_data()
        data["hynix"]["current_price"] = None
        result = predict_hynix_signal(data)
        assert result["disclaimer"]
