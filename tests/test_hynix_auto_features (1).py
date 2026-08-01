"""
test_hynix_auto_features.py — hynix_auto_features 모듈 테스트.

수동 입력 없이 자동으로 feature를 생성할 수 있는지 검증합니다.
"""

from __future__ import annotations

import pytest
import pandas as pd
from datetime import datetime, timedelta

from app.features.hynix_auto_features import (
    build_auto_features,
    _compute_data_quality,
    _build_tech_indicators,
)


def _make_hynix_daily(n: int = 70) -> pd.DataFrame:
    rows = []
    price = 200_000.0
    for i in range(n):
        price = price * (1.0 + 0.002 * (i % 3 - 1))
        rows.append({
            "datetime": datetime(2026, 1, 1) + timedelta(days=i),
            "open":   price * 0.998,
            "high":   price * 1.008,
            "low":    price * 0.992,
            "close":  price,
            "volume": 5_000_000,
        })
    return pd.DataFrame(rows)


def _make_market_data(
    has_mu: bool = True,
    has_klab: bool = True,
    has_hynix: bool = True,
    has_nvda: bool = True,
    has_index: bool = True,
) -> dict:
    mu_df = pd.DataFrame({
        "datetime": pd.date_range("2026-06-29 17:00", periods=30, freq="1min"),
        "open":  [102.0] * 30,
        "high":  [103.0] * 30,
        "low":   [101.0] * 30,
        "close": [102.5] * 30,
        "volume": [100_000] * 30,
        "session": ["premarket"] * 30,
    }) if has_mu else None

    return {
        "mu": {
            "df_1min": mu_df,
            "df_3min": None,
            "current_price": {"price": 102.5, "open": 102.0, "high": 103.0, "low": 101.0} if has_mu else None,
            "source": "yfinance" if has_mu else None,
            "error": None,
        },
        "nvda": {
            "current_price": 120.0 if has_nvda else None,
            "premarket_return": None,
            "regular_return": 2.5 if has_nvda else None,
            "source": "yfinance" if has_nvda else None,
            "error": None,
        },
        "index": {
            "qqq_return":    1.2 if has_index else None,
            "sox_return":    1.8 if has_index else None,
            "usdkrw_change": 0.3 if has_index else None,
            "source": "yfinance" if has_index else None,
            "error": None,
        },
        "hynix": {
            "df_daily":  _make_hynix_daily() if has_hynix else None,
            "prev_close": 195_000.0 if has_hynix else None,
            "source": "yfinance" if has_hynix else None,
            "error": None,
        },
        "kospilab": {
            "hynix_reference_price":    198_000.0 if has_klab else None,
            "hynix_reference_return":   1.5 if has_klab else None,
            "samsung_reference_return": 0.8 if has_klab else None,
            "hyundai_reference_return": -0.5 if has_klab else None,
            "source_status": "success" if has_klab else "failed",
            "error_message": None,
        },
        "collected_at": "2026-06-29T09:00:00",
        "errors": [],
    }


class TestBuildAutoFeatures:
    """build_auto_features 함수 테스트."""

    def test_returns_required_top_level_keys(self):
        market = _make_market_data()
        result = build_auto_features(market)
        for key in ("micron_features", "predictor_kwargs", "swing_kwargs",
                    "tech_indicators", "hynix_prev_close", "data_quality", "sources"):
            assert key in result

    def test_micron_features_has_11_keys(self):
        market = _make_market_data()
        result = build_auto_features(market)
        mf = result["micron_features"]
        assert len(mf) == 11

    def test_predictor_kwargs_contains_kospilab(self):
        market = _make_market_data(has_klab=True)
        result = build_auto_features(market)
        assert result["predictor_kwargs"]["kospilab_expected_return_pct"] == pytest.approx(1.5)

    def test_predictor_kwargs_kospilab_none_when_failed(self):
        market = _make_market_data(has_klab=False)
        result = build_auto_features(market)
        assert result["predictor_kwargs"]["kospilab_expected_return_pct"] is None

    def test_data_quality_between_0_and_1(self):
        market = _make_market_data()
        result = build_auto_features(market)
        assert 0 <= result["data_quality"] <= 1

    def test_data_quality_low_when_no_data(self):
        market = _make_market_data(has_mu=False, has_klab=False, has_hynix=False, has_nvda=False, has_index=False)
        result = build_auto_features(market)
        assert result["data_quality"] < 0.2

    def test_data_quality_high_when_all_present(self):
        market = _make_market_data()
        result = build_auto_features(market)
        assert result["data_quality"] >= 0.5

    def test_no_crash_on_empty_market_data(self):
        result = build_auto_features({})
        assert "micron_features" in result
        assert result["data_quality"] == 0.0

    def test_tech_indicators_computed_from_hynix_daily(self):
        market = _make_market_data(has_hynix=True)
        result = build_auto_features(market)
        ti = result["tech_indicators"]
        # 70일 일봉이 있으므로 RSI가 계산되어야 함
        assert ti.get("rsi_14") is not None

    def test_hynix_prev_close_propagated(self):
        market = _make_market_data(has_hynix=True)
        result = build_auto_features(market)
        assert result["hynix_prev_close"] == pytest.approx(195_000.0)

    def test_sources_dict_present(self):
        market = _make_market_data()
        result = build_auto_features(market)
        sources = result["sources"]
        for k in ("mu", "nvda", "index", "hynix", "kospilab"):
            assert k in sources


class TestDataQuality:
    """_compute_data_quality 함수 개별 테스트."""

    def test_all_present_gives_high_score(self):
        mf   = {"micron_session_strength_score": 70.0}
        pkw  = {"kospilab_expected_return_pct": 1.5, "nvda_return_pct": 2.0,
                 "sox_return_pct": 1.8, "hynix_prev_close": 195_000.0}
        ti   = {"rsi_14": 45.0}
        score = _compute_data_quality(mf, pkw, ti)
        assert score == pytest.approx(1.0)

    def test_nothing_gives_zero(self):
        mf   = {"micron_session_strength_score": None}
        pkw  = {"kospilab_expected_return_pct": None, "nvda_return_pct": None,
                 "sox_return_pct": None, "hynix_prev_close": None}
        ti   = {"rsi_14": None}
        score = _compute_data_quality(mf, pkw, ti)
        assert score == pytest.approx(0.0)

    def test_partial_gives_partial_score(self):
        mf   = {"micron_session_strength_score": 60.0}  # 0.30
        pkw  = {"kospilab_expected_return_pct": None, "nvda_return_pct": None,
                 "sox_return_pct": None, "hynix_prev_close": None}
        ti   = {"rsi_14": None}
        score = _compute_data_quality(mf, pkw, ti)
        assert score == pytest.approx(0.30)


class TestBuildTechIndicators:
    """_build_tech_indicators 함수 테스트."""

    def test_returns_all_keys(self):
        df = _make_hynix_daily(70)
        ti = _build_tech_indicators(df)
        expected_keys = [
            "rsi_14", "macd", "macd_signal_cross", "ma5_position_pct",
            "ma20_position_pct", "ma60_position_pct", "from_20d_high_pct",
            "from_20d_low_pct", "bollinger_pct", "prev_candle_type",
            "return_3d_pct", "return_5d_pct", "return_10d_pct", "volume_change_pct",
        ]
        for k in expected_keys:
            assert k in ti

    def test_none_df_returns_all_none(self):
        ti = _build_tech_indicators(None)
        assert all(v is None for v in ti.values())

    def test_empty_df_returns_all_none(self):
        ti = _build_tech_indicators(pd.DataFrame())
        assert all(v is None for v in ti.values())
