"""
test_hynix_full_auto_prediction.py — 완전 자동 예측 통합 테스트.

수동 입력 없이 자동 수집 데이터만으로 예측이 실행되는지 검증합니다.
"""

from __future__ import annotations

import pytest
import pandas as pd
from datetime import datetime, timedelta
from unittest.mock import patch

from app.features.hynix_auto_features import build_auto_features
from app.models.hynix_predictor import predict_hynix
from app.models.hynix_swing_flag import evaluate_swing_flag


def _make_daily_df(n: int = 70) -> pd.DataFrame:
    rows = []
    price = 195_000.0
    for i in range(n):
        price = price * (1.0 + 0.001 * (i % 5 - 2))
        rows.append({
            "datetime": datetime(2026, 1, 1) + timedelta(days=i),
            "open":   price * 0.998,
            "high":   price * 1.009,
            "low":    price * 0.992,
            "close":  price,
            "volume": 5_000_000 + i * 20_000,
        })
    return pd.DataFrame(rows)


def _make_full_market_data() -> dict:
    mu_df = pd.DataFrame({
        "datetime": pd.date_range("2026-06-29 17:00", periods=60, freq="1min"),
        "open":   [101.5] * 60,
        "high":   [103.0] * 60,
        "low":    [100.5] * 60,
        "close":  [102.0] * 60,
        "volume": [80_000]  * 60,
        "session": ["premarket"] * 60,
    })
    return {
        "mu": {
            "df_1min": mu_df,
            "df_3min": None,
            "current_price": {"price": 102.0, "open": 101.5, "high": 103.0, "low": 100.5},
            "source": "yfinance",
            "error": None,
        },
        "nvda": {
            "current_price": 120.0,
            "premarket_return": None,
            "regular_return": 1.8,
            "source": "yfinance",
            "error": None,
        },
        "index": {
            "qqq_return": 1.0,
            "sox_return": 1.5,
            "usdkrw_change": 0.2,
            "source": "yfinance",
            "error": None,
        },
        "hynix": {
            "df_daily": _make_daily_df(),
            "prev_close": 195_000.0,
            "source": "yfinance",
            "error": None,
        },
        "kospilab": {
            "hynix_reference_price": 197_000.0,
            "hynix_reference_return": 1.0,
            "samsung_reference_return": 0.5,
            "hyundai_reference_return": -0.2,
            "source_status": "success",
            "error_message": None,
        },
        "collected_at": "2026-06-29T09:00:00",
        "errors": [],
    }


def _make_empty_market_data() -> dict:
    """모든 수집이 실패한 케이스."""
    return {
        "mu":       {"df_1min": None, "df_3min": None, "current_price": None, "source": None, "error": "failed"},
        "nvda":     {"current_price": None, "premarket_return": None, "regular_return": None, "source": None, "error": "failed"},
        "index":    {"qqq_return": None, "sox_return": None, "usdkrw_change": None, "source": None, "error": "failed"},
        "hynix":    {"df_daily": None, "prev_close": None, "source": None, "error": "failed"},
        "kospilab": {"hynix_reference_price": None, "hynix_reference_return": None,
                     "source_status": "failed", "error_message": "네트워크 오류"},
        "collected_at": "2026-06-29T09:00:00",
        "errors": ["MU: failed", "코스피랩: 네트워크 오류"],
    }


class TestFullAutoPipelineSuccess:
    """완전 자동 파이프라인 — 데이터가 모두 있는 정상 케이스."""

    def test_pipeline_runs_without_manual_input(self):
        market = _make_full_market_data()
        auto_feat = build_auto_features(market)
        pred = predict_hynix(
            micron_features=auto_feat["micron_features"],
            **auto_feat["predictor_kwargs"],
        )
        assert pred is not None

    def test_prediction_has_required_fields(self):
        market = _make_full_market_data()
        auto_feat = build_auto_features(market)
        pred = predict_hynix(
            micron_features=auto_feat["micron_features"],
            **auto_feat["predictor_kwargs"],
        )
        for key in ("today_return_pct", "tomorrow_return_pct", "day3_return_pct",
                    "up_probability", "down_probability", "confidence_score"):
            assert key in pred

    def test_swing_flag_generated(self):
        market = _make_full_market_data()
        auto_feat = build_auto_features(market)
        pred = predict_hynix(
            micron_features=auto_feat["micron_features"],
            **auto_feat["predictor_kwargs"],
        )
        swing = evaluate_swing_flag(
            micron_features=auto_feat["micron_features"],
            prediction=pred,
            **auto_feat["swing_kwargs"],
        )
        assert 0 <= swing["swing_score"] <= 100
        assert swing["swing_flag"] is not None

    def test_confidence_lower_without_manual_input(self):
        """자동 수집만으로도 신뢰도 점수가 양수여야 함."""
        market = _make_full_market_data()
        auto_feat = build_auto_features(market)
        pred = predict_hynix(
            micron_features=auto_feat["micron_features"],
            **auto_feat["predictor_kwargs"],
        )
        assert pred["confidence_score"] >= 0

    def test_data_quality_score_high(self):
        market = _make_full_market_data()
        auto_feat = build_auto_features(market)
        assert auto_feat["data_quality"] >= 0.7


class TestFullAutoPipelineFailure:
    """완전 자동 파이프라인 — 모든 수집이 실패한 케이스."""

    def test_pipeline_does_not_crash_when_all_data_missing(self):
        market = _make_empty_market_data()
        auto_feat = build_auto_features(market)
        pred = predict_hynix(
            micron_features=auto_feat["micron_features"],
            **auto_feat["predictor_kwargs"],
        )
        assert pred is not None

    def test_swing_flag_does_not_crash_when_all_data_missing(self):
        market = _make_empty_market_data()
        auto_feat = build_auto_features(market)
        swing = evaluate_swing_flag(
            micron_features=auto_feat["micron_features"],
            **auto_feat["swing_kwargs"],
        )
        assert 0 <= swing["swing_score"] <= 100

    def test_data_quality_low_when_all_failed(self):
        market = _make_empty_market_data()
        auto_feat = build_auto_features(market)
        assert auto_feat["data_quality"] < 0.2

    def test_prediction_fields_present_even_with_no_data(self):
        market = _make_empty_market_data()
        auto_feat = build_auto_features(market)
        pred = predict_hynix(
            micron_features=auto_feat["micron_features"],
            **auto_feat["predictor_kwargs"],
        )
        for key in ("today_return_pct", "up_probability", "confidence_score"):
            assert key in pred


class TestKisApiFallback:
    """KIS API 실패 시 yfinance fallback 검증."""

    def test_prediction_works_with_yfinance_fallback(self):
        """KIS 없이 yfinance만으로 전체 파이프라인 동작."""
        market = _make_full_market_data()
        market["mu"]["source"] = "yfinance"
        market["hynix"]["source"] = "yfinance"
        auto_feat = build_auto_features(market)
        pred = predict_hynix(
            micron_features=auto_feat["micron_features"],
            **auto_feat["predictor_kwargs"],
        )
        assert pred is not None


class TestMinuteBarsResample:
    """1분봉 → 3분봉 리샘플링 검증."""

    def test_3min_bars_generated_from_1min(self):
        from app.data_sources.auto_market_collector import collect_mu_data
        mu_df = pd.DataFrame({
            "datetime": pd.date_range("2026-06-29 17:00", periods=30, freq="1min"),
            "open":   [100.0 + i * 0.02 for i in range(30)],
            "high":   [101.0 + i * 0.02 for i in range(30)],
            "low":    [99.0 + i * 0.02 for i in range(30)],
            "close":  [100.5 + i * 0.02 for i in range(30)],
            "volume": [50_000 + i for i in range(30)],
        })
        mu_daily = pd.DataFrame({
            "datetime": pd.date_range("2026-05-01", periods=30, freq="B"),
            "open": [100.0 + i * 0.1 for i in range(30)],
            "high": [101.0 + i * 0.1 for i in range(30)],
            "low": [99.0 + i * 0.1 for i in range(30)],
            "close": [100.5 + i * 0.1 for i in range(30)],
            "volume": [1_000_000 + i for i in range(30)],
        })

        with patch("app.data_sources.auto_market_collector._kis_mode", return_value=None):
            with patch("app.data_sources.auto_market_collector._fetch_yfinance_intraday", return_value=mu_df):
                with patch("app.data_sources.auto_market_collector._fetch_yfinance_daily", return_value=mu_daily):
                    result = collect_mu_data()

        assert result["df_3min"] is not None
        assert not result["df_3min"].empty
        # 30분 데이터 → 10개 3분봉
        assert len(result["df_3min"]) == 10
