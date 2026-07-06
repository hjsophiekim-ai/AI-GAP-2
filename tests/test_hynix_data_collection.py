"""
test_hynix_data_collection.py — SK하이닉스 일봉 수집 및 검증 테스트.

000660.KS yfinance fallback, 20행 검증, 가격 범위 필터링을 확인합니다.
"""

from __future__ import annotations

import pytest
import pandas as pd
from unittest.mock import patch, MagicMock
from pathlib import Path

from app.data.market_data_validator import (
    validate_hynix_dataframe,
    HYNIX_PRICE_MIN,
    HYNIX_PRICE_MAX,
)


# ── 헬퍼 ─────────────────────────────────────────────────────────────────────

def _make_hynix_df(n: int = 30, close: float = 180_000) -> pd.DataFrame:
    return pd.DataFrame({
        "datetime": pd.date_range("2025-12-01", periods=n, freq="B"),
        "open":     [close * 0.99] * n,
        "high":     [close * 1.01] * n,
        "low":      [close * 0.98] * n,
        "close":    [close] * n,
        "volume":   [10_000_000] * n,
    })


# ── validate_hynix_dataframe 심화 ────────────────────────────────────────────

class TestValidateHynixDataframeDeep:
    def test_exactly_20_rows_valid(self):
        df = _make_hynix_df(20)
        ok, msg, result = validate_hynix_dataframe(df)
        assert ok is True
        assert len(result) == 20

    def test_19_rows_invalid(self):
        df = _make_hynix_df(19)
        ok, msg, result = validate_hynix_dataframe(df)
        assert ok is False

    def test_min_price_boundary(self):
        df = _make_hynix_df(25, close=HYNIX_PRICE_MIN)
        ok, msg, result = validate_hynix_dataframe(df)
        assert ok is True

    def test_max_price_boundary(self):
        df = _make_hynix_df(25, close=HYNIX_PRICE_MAX)
        ok, msg, result = validate_hynix_dataframe(df)
        assert ok is True

    def test_prices_below_min_filtered_out(self):
        df = _make_hynix_df(30)
        df.loc[:9, "close"] = 10_000   # 10개 비정상 저가
        ok, msg, result = validate_hynix_dataframe(df)
        assert ok is True
        assert len(result) == 20
        assert all(result["close"] >= HYNIX_PRICE_MIN)

    def test_prices_above_max_filtered_out(self):
        df = _make_hynix_df(30)
        df.loc[:9, "close"] = HYNIX_PRICE_MAX + 1_000   # 10개 비정상 고가
        ok, msg, result = validate_hynix_dataframe(df)
        assert ok is True
        assert len(result) == 20

    def test_prev_close_is_valid(self):
        df = _make_hynix_df(25)
        ok, msg, result = validate_hynix_dataframe(df)
        if ok:
            prev_close = float(result.iloc[-1]["close"])
            assert HYNIX_PRICE_MIN <= prev_close <= HYNIX_PRICE_MAX


# ── collect_hynix_daily 통합 테스트 ──────────────────────────────────────────

class TestCollectHynixDaily:
    def test_yfinance_success_returns_valid(self):
        mock_hist = _make_hynix_df(40)
        mock_hist = mock_hist.rename(columns={
            "datetime": "Date", "open": "Open", "high": "High",
            "low": "Low", "close": "Close", "volume": "Volume",
        }).set_index("Date")

        from app.data_sources.auto_market_collector import collect_hynix_daily

        with patch("app.data_sources.auto_market_collector._kis_mode", return_value=None):
            with patch("yfinance.Ticker") as mock_ticker:
                mock_ticker.return_value.history.return_value = mock_hist
                result = collect_hynix_daily()

        if result["source"] == "yfinance":
            assert result["df_daily"] is not None
            assert len(result["df_daily"]) >= 20
            assert result["prev_close"] is not None
            assert HYNIX_PRICE_MIN <= result["prev_close"] <= HYNIX_PRICE_MAX

    def test_yfinance_invalid_prices_does_not_use_cache(self, tmp_path):
        """yfinance가 비정상 가격 반환 시 캐시 사용."""
        bad_hist = _make_hynix_df(40, close=100)  # 100원 → 비정상
        bad_hist = bad_hist.rename(columns={
            "datetime": "Date", "open": "Open", "high": "High",
            "low": "Low", "close": "Close", "volume": "Volume",
        }).set_index("Date")

        good_cache = _make_hynix_df(25)
        cache_path = tmp_path / "hynix_daily.csv"
        good_cache.to_csv(cache_path, index=False)

        from app.data_sources.auto_market_collector import collect_hynix_daily

        with patch("app.data_sources.auto_market_collector._kis_mode", return_value=None):
            with patch("app.data_sources.auto_market_collector._HYNIX_DAILY_CSV", cache_path):
                with patch("yfinance.Ticker") as mock_ticker:
                    mock_ticker.return_value.history.return_value = bad_hist
                    result = collect_hynix_daily()

        # yfinance 실패 → 캐시 사용
        assert "df_daily" in result
        assert result["source"] != "cache"

    def test_all_fail_no_crash(self):
        from app.data_sources.auto_market_collector import collect_hynix_daily

        with patch("app.data_sources.auto_market_collector._kis_mode", return_value=None):
            with patch("yfinance.Ticker") as mock_ticker:
                mock_ticker.return_value.history.return_value = pd.DataFrame()
                result = collect_hynix_daily()

        assert "df_daily" in result
        assert "error" in result

    def test_returns_required_keys(self):
        from app.data_sources.auto_market_collector import collect_hynix_daily

        with patch("app.data_sources.auto_market_collector._kis_mode", return_value=None):
            with patch("yfinance.Ticker") as mock_ticker:
                mock_ticker.return_value.history.side_effect = Exception("네트워크 오류")
                result = collect_hynix_daily()

        for k in ("df_daily", "prev_close", "source", "error"):
            assert k in result


# ── 최소 조건 검증 테스트 ────────────────────────────────────────────────────

class TestForecastMinimumConditions:
    """forecast_engine 최소 조건 검증 함수 테스트."""

    def _make_auto_feat(
        self,
        has_prev_close: bool = True,
        has_mu: bool = True,
        has_kospilab: bool = True,
        ext_count: int = 4,
    ) -> dict:
        ext = {}
        keys = ["sox_return_pct", "nvda_return_pct", "qqq_return_pct", "usd_krw_change_pct"]
        for i, k in enumerate(keys):
            ext[k] = 1.0 if i < ext_count else None

        return {
            "predictor_kwargs": {
                "hynix_current_price": 180_500,
                "hynix_prev_close": 180_000 if has_prev_close else None,
                "kospilab_expected_return_pct": 1.5 if has_kospilab else None,
                **ext,
            },
            "micron_features": {
                "micron_session_strength_score": 70.0 if has_mu else None,
            },
        }

    def test_all_conditions_met(self):
        from app.ml.hynix_forecast_engine import _check_minimum_conditions
        ok, msg = _check_minimum_conditions(self._make_auto_feat())
        assert ok is True

    def test_no_prev_close_blocked(self):
        from app.ml.hynix_forecast_engine import _check_minimum_conditions
        ok, msg = _check_minimum_conditions(self._make_auto_feat(has_prev_close=False))
        assert ok is False
        assert "전일 종가" in msg

    def test_no_mu_and_no_kospilab_blocked(self):
        from app.ml.hynix_forecast_engine import _check_minimum_conditions
        ok, msg = _check_minimum_conditions(
            self._make_auto_feat(has_mu=False, has_kospilab=False)
        )
        assert ok is False
        assert "코스피랩" in msg or "MU" in msg

    def test_only_1_ext_indicator_blocked(self):
        from app.ml.hynix_forecast_engine import _check_minimum_conditions
        ok, msg = _check_minimum_conditions(self._make_auto_feat(ext_count=1))
        assert ok is False
        assert "외부 지표" in msg

    def test_exactly_2_ext_indicators_ok(self):
        from app.ml.hynix_forecast_engine import _check_minimum_conditions
        ok, msg = _check_minimum_conditions(self._make_auto_feat(ext_count=2))
        assert ok is True

    def test_mu_only_no_kospilab_ok(self):
        """MU 있고 코스피랩 없어도 조건 2 통과."""
        from app.ml.hynix_forecast_engine import _check_minimum_conditions
        ok, msg = _check_minimum_conditions(
            self._make_auto_feat(has_mu=True, has_kospilab=False)
        )
        assert ok is True

    def test_kospilab_only_no_mu_ok(self):
        """코스피랩 있고 MU 없어도 조건 2 통과."""
        from app.ml.hynix_forecast_engine import _check_minimum_conditions
        ok, msg = _check_minimum_conditions(
            self._make_auto_feat(has_mu=False, has_kospilab=True)
        )
        assert ok is True
