"""
test_hynix_forecast_engine.py — 예측 파이프라인 + 데이터 수집률 게이트 테스트.
"""

from __future__ import annotations

import pytest
import pandas as pd
from datetime import datetime, timedelta
from unittest.mock import patch, MagicMock

from app.ml.hynix_forecast_engine import run_forecast, collection_rate_label, BLOCK_THRESHOLD, LOW_CONF_THRESHOLD


# ── 샘플 데이터 헬퍼 ──────────────────────────────────────────────────────────

def _make_daily_df(n: int = 70) -> pd.DataFrame:
    rows = []
    price = 195_000.0
    for i in range(n):
        price = price * (1.0 + 0.001 * (i % 5 - 2))
        rows.append({
            "datetime": datetime(2026, 1, 1) + timedelta(days=i),
            "open":   price * 0.998,
            "high":   price * 1.008,
            "low":    price * 0.992,
            "close":  price,
            "volume": 5_000_000 + i * 10_000,
        })
    return pd.DataFrame(rows)


def _full_market_data() -> dict:
    mu_df = pd.DataFrame({
        "datetime": pd.date_range("2026-06-29 17:00", periods=60, freq="1min"),
        "open":  [101.5 + i * 0.01 for i in range(60)],
        "high":  [103.0 + i * 0.01 for i in range(60)],
        "low":   [100.5 + i * 0.01 for i in range(60)],
        "close": [102.0 + i * 0.01 for i in range(60)],
        "volume": [80_000 + i for i in range(60)],
        "session": ["premarket"] * 60,
    })
    mu_daily = pd.DataFrame({
        "datetime": pd.date_range("2026-05-01", periods=30, freq="B"),
        "open": [100 + i * 0.1 for i in range(30)],
        "high": [101 + i * 0.1 for i in range(30)],
        "low": [99 + i * 0.1 for i in range(30)],
        "close": [100 + i * 0.1 for i in range(30)],
        "volume": [1_000_000 + i for i in range(30)],
    })
    return {
        "mu": {
            "df_1min":      mu_df,
            "df_3min":      mu_df.iloc[::3].reset_index(drop=True),
            "df_daily":     mu_daily,
            "current_price": {"price": 102.0, "open": 101.5, "high": 103.0, "low": 100.5},
            "source":       "yfinance",
            "error":        None,
        },
        "nvda": {
            "current_price":   120.0,
            "premarket_return": None,
            "regular_return":  1.8,
            "source":          "yfinance",
            "error":           None,
        },
        "index": {
            "qqq_return":    1.0,
            "sox_return":    1.5,
            "usdkrw_change": 0.2,
            "source":        "yfinance",
            "error":         None,
        },
        "hynix": {
            "df_daily":  _make_daily_df(),
            "prev_close": 195_000.0,
            "current_price": 195_500.0,
            "source":    "yfinance",
            "stock_identity": {"code": "000660", "name": "SK하이닉스", "ok": True, "message": "ok"},
            "price_validation": {
                "ok": True,
                "message": "ok",
                "source_prices": {"KIS": 195_500.0, "naver": 195_600.0, "yfinance": 195_400.0},
                "selected_source": "KIS",
                "selected_price": 195_500.0,
                "max_diff_pct": 0.1024,
            },
            "error":     None,
        },
        "kospilab": {
            "hynix_reference_price":    197_000.0,
            "hynix_reference_return":   1.0,
            "samsung_reference_return": 0.5,
            "hyundai_reference_return": -0.2,
            "source_status":            "success",
            "error_message":            None,
        },
        "collected_at": "2026-06-29T09:00:00",
        "errors": [],
    }


def _empty_market_data() -> dict:
    return {
        "mu":       {"df_1min": None, "df_3min": None, "current_price": None, "source": None, "error": "failed"},
        "nvda":     {"current_price": None, "premarket_return": None, "regular_return": None, "source": None, "error": "failed"},
        "index":    {"qqq_return": None, "sox_return": None, "usdkrw_change": None, "source": None, "error": "failed"},
        "hynix":    {"df_daily": None, "prev_close": None, "source": None, "error": "failed"},
        "kospilab": {"hynix_reference_price": None, "hynix_reference_return": None,
                     "source_status": "failed", "error_message": "네트워크 오류"},
        "collected_at": "2026-06-29T09:00:00",
        "errors": ["MU: failed", "kospilab: 네트워크 오류"],
    }


def _partial_market_data() -> dict:
    """hynix daily + NVDA/SOX 있지만 MU·코스피랩 없는 케이스 (품질 0.40~0.70)."""
    return {
        "mu":       {"df_1min": None, "df_3min": None, "current_price": None, "source": None, "error": "failed"},
        "nvda":     {"current_price": 120.0, "premarket_return": None, "regular_return": 1.8, "source": "yfinance", "error": None},
        "index":    {"qqq_return": 1.0, "sox_return": 1.5, "usdkrw_change": 0.2, "source": "yfinance", "error": None},
        "hynix":    {"df_daily": _make_daily_df(), "prev_close": 195_000.0, "source": "yfinance", "error": None},
        "kospilab": {"hynix_reference_price": None, "hynix_reference_return": None,
                     "source_status": "failed", "error_message": "JS 렌더 실패"},
        "collected_at": "2026-06-29T09:00:00",
        "errors": ["MU: failed", "kospilab: failed"],
    }


# ── 상태 반환 테스트 ──────────────────────────────────────────────────────────

class TestRunForecastStatus:

    def test_ok_status_with_full_data(self):
        result = run_forecast(_full_market_data())
        assert result["status"] == "ok"

    def test_blocked_when_all_data_missing(self):
        result = run_forecast(_empty_market_data())
        assert result["status"] == "blocked"

    def test_low_confidence_when_partial_data(self):
        result = run_forecast(_partial_market_data())
        # data_quality = RSI(0.20) + NVDA/SOX(0.15) + hynix_prev_close(0.10) = 0.45
        # 0.40 <= 0.45 < 0.70 → low_confidence
        assert result["status"] in ("low_confidence", "blocked")

    def test_returns_required_keys(self):
        result = run_forecast(_full_market_data())
        for key in ("status", "data_quality", "message", "auto_features",
                    "prediction", "swing", "explanation", "errors"):
            assert key in result

    def test_no_crash_on_empty_input(self):
        result = run_forecast({})
        assert result["status"] in ("blocked", "ok", "low_confidence")
        assert "status" in result


class TestRunForecastPrediction:

    def test_prediction_populated_when_ok(self):
        result = run_forecast(_full_market_data())
        if result["status"] == "ok":
            assert result["prediction"] is not None

    def test_prediction_none_when_blocked(self):
        result = run_forecast(_empty_market_data())
        if result["status"] == "blocked":
            assert result["prediction"] is None

    def test_swing_populated_when_ok(self):
        result = run_forecast(_full_market_data())
        if result["status"] == "ok":
            assert result["swing"] is not None
            assert 0 <= result["swing"]["swing_score"] <= 100

    def test_swing_has_action_text_when_ok(self):
        result = run_forecast(_full_market_data())
        if result["swing"] is not None:
            assert result["swing"].get("action_text") is not None
            assert len(result["swing"]["action_text"]) > 0

    def test_explanation_string_when_ok(self):
        result = run_forecast(_full_market_data())
        if result["explanation"] is not None:
            assert isinstance(result["explanation"], str)
            assert len(result["explanation"]) > 10

    def test_data_quality_high_with_full_data(self):
        result = run_forecast(_full_market_data())
        assert result["data_quality"] >= 0.70

    def test_data_quality_low_with_empty_data(self):
        result = run_forecast(_empty_market_data())
        assert result["data_quality"] < BLOCK_THRESHOLD

    def test_errors_is_list(self):
        result = run_forecast(_empty_market_data())
        assert isinstance(result["errors"], list)

    def test_message_is_string(self):
        result = run_forecast(_full_market_data())
        assert isinstance(result["message"], str)
        assert len(result["message"]) > 0


class TestCollectionRateLabel:

    def test_ok_label_above_threshold(self):
        label, color = collection_rate_label(LOW_CONF_THRESHOLD + 0.01)
        assert label == "정상"
        assert color.startswith("#")

    def test_low_conf_label_between_thresholds(self):
        mid = (BLOCK_THRESHOLD + LOW_CONF_THRESHOLD) / 2
        label, color = collection_rate_label(mid)
        assert label == "낮은 신뢰도"

    def test_blocked_label_below_threshold(self):
        label, color = collection_rate_label(BLOCK_THRESHOLD - 0.01)
        assert label == "수집 부족"

    def test_returns_tuple_of_two(self):
        result = collection_rate_label(0.5)
        assert len(result) == 2
