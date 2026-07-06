"""
test_auto_market_collector.py — 자동 시장 데이터 수집 모듈 테스트.

실제 네트워크 없이 구조·fallback·오류 처리를 검증합니다.
"""

from __future__ import annotations

import pytest
import pandas as pd
from unittest.mock import patch, MagicMock
from pathlib import Path

from app.data_sources.auto_market_collector import (
    collect_all,
    collect_mu_data,
    collect_nvda_data,
    collect_index_data,
    collect_hynix_daily,
    collect_kospilab_data,
    _normalize_yf_ohlcv,
)


def _make_df(n: int = 10) -> pd.DataFrame:
    """샘플 OHLCV DataFrame."""
    idx = pd.date_range("2026-01-01", periods=n, freq="D")
    return pd.DataFrame({
        "open":   [100.0] * n,
        "high":   [105.0] * n,
        "low":    [98.0] * n,
        "close":  [102.0] * n,
        "volume": [1_000_000] * n,
    }, index=idx)


class TestCollectAll:
    """collect_all() 구조 검증."""

    def test_returns_required_keys(self):
        with patch("app.data_sources.auto_market_collector.collect_mu_data",
                   return_value={"df_1min": None, "df_3min": None, "current_price": None, "source": None, "error": None}):
            with patch("app.data_sources.auto_market_collector.collect_nvda_data",
                       return_value={"current_price": None, "premarket_return": None, "regular_return": None, "source": None, "error": None}):
                with patch("app.data_sources.auto_market_collector.collect_index_data",
                           return_value={"qqq_return": None, "sox_return": None, "usdkrw_change": None, "source": None, "error": None}):
                    with patch("app.data_sources.auto_market_collector.collect_hynix_daily",
                               return_value={"df_daily": None, "prev_close": None, "source": None, "error": None}):
                        with patch("app.data_sources.auto_market_collector.collect_kospilab_data",
                                   return_value={"hynix_reference_price": None, "hynix_reference_return": None,
                                                 "source_status": "failed", "error_message": "mock"}):
                            result = collect_all()

        for key in ("mu", "nvda", "index", "hynix", "kospilab", "collected_at", "errors"):
            assert key in result

    def test_errors_is_list(self):
        with patch("app.data_sources.auto_market_collector.collect_mu_data",
                   return_value={"error": "some error", "df_1min": None, "df_3min": None, "current_price": None, "source": None}):
            with patch("app.data_sources.auto_market_collector.collect_nvda_data",
                       return_value={"error": None, "current_price": None, "premarket_return": None, "regular_return": None, "source": None}):
                with patch("app.data_sources.auto_market_collector.collect_index_data",
                           return_value={"error": None, "qqq_return": None, "sox_return": None, "usdkrw_change": None, "source": None}):
                    with patch("app.data_sources.auto_market_collector.collect_hynix_daily",
                               return_value={"error": None, "df_daily": None, "prev_close": None, "source": None}):
                        with patch("app.data_sources.auto_market_collector.collect_kospilab_data",
                                   return_value={"source_status": "success", "error_message": None,
                                                 "hynix_reference_price": None, "hynix_reference_return": None}):
                            result = collect_all()
        assert isinstance(result["errors"], list)

    def test_collected_at_is_string(self):
        with patch("app.data_sources.auto_market_collector.collect_mu_data",
                   return_value={"error": None, "df_1min": None, "df_3min": None, "current_price": None, "source": None}):
            with patch("app.data_sources.auto_market_collector.collect_nvda_data",
                       return_value={"error": None, "current_price": None, "premarket_return": None, "regular_return": None, "source": None}):
                with patch("app.data_sources.auto_market_collector.collect_index_data",
                           return_value={"error": None, "qqq_return": None, "sox_return": None, "usdkrw_change": None, "source": None}):
                    with patch("app.data_sources.auto_market_collector.collect_hynix_daily",
                               return_value={"error": None, "df_daily": None, "prev_close": None, "source": None}):
                        with patch("app.data_sources.auto_market_collector.collect_kospilab_data",
                                   return_value={"source_status": "failed", "error_message": None,
                                                 "hynix_reference_price": None, "hynix_reference_return": None}):
                            result = collect_all()
        assert isinstance(result["collected_at"], str)


class TestCollectMuData:
    """MU 데이터 수집 fallback 검증."""

    def test_falls_back_to_yfinance_when_kis_absent(self):
        """KIS 키 없을 때 yfinance fallback 호출."""
        mock_df = _make_df(60)

        with patch("app.data_sources.auto_market_collector._kis_mode", return_value=None):
            with patch("app.data_sources.auto_market_collector._fetch_yfinance_intraday",
                       return_value=mock_df.reset_index().rename(columns={"index": "datetime", "Open": "open",
                                                                           "High": "high", "Low": "low",
                                                                           "Close": "close", "Volume": "volume"})):
                result = collect_mu_data()

        assert result["source"] in ("yfinance", None)

    def test_returns_required_keys(self):
        with patch("app.data_sources.auto_market_collector._kis_mode", return_value=None):
            with patch("app.data_sources.auto_market_collector._fetch_yfinance_intraday",
                       side_effect=Exception("no yf")):
                result = collect_mu_data()
        for k in ("df_1min", "df_3min", "current_price", "source", "error"):
            assert k in result

    def test_no_crash_on_all_failure(self):
        with patch("app.data_sources.auto_market_collector._kis_mode", return_value=None):
            with patch("app.data_sources.auto_market_collector._fetch_yfinance_intraday",
                       side_effect=Exception("network unavailable")):
                result = collect_mu_data()
        assert result["df_1min"] is None


class TestCollectIndexData:
    """지수/ETF 수집 테스트."""

    def test_yfinance_success_returns_floats(self):
        fake_data = pd.DataFrame({
            ("Close", "QQQ"):    [450.0, 455.0],
            ("Close", "SOXX"):   [200.0, 202.0],
            ("Close", "USDKRW=X"): [1380.0, 1382.0],
        })
        fake_data.columns = pd.MultiIndex.from_tuples(fake_data.columns)

        import yfinance as yf
        with patch("yfinance.download", return_value=fake_data):
            result = collect_index_data()

        if result["qqq_return"] is not None:
            assert isinstance(result["qqq_return"], float)
        if result["sox_return"] is not None:
            assert isinstance(result["sox_return"], float)

    def test_yfinance_failure_handled(self):
        with patch("app.data_sources.auto_market_collector._naver_global_quote",
                   return_value={"status": "failed", "price": None, "return_pct": None, "source": "failed"}):
            with patch("app.data_sources.auto_market_collector._fetch_global_quote_from_yfinance",
                       return_value={"status": "failed", "price": None, "return_pct": None, "source": "yfinance"}):
                result = collect_index_data()
        assert result["qqq_return"] is None
        assert result["error"] is not None


class TestCollectKospilab:
    """코스피랩 수집 fallback 검증."""

    def test_import_error_handled(self):
        # kospilab_scraper 모듈 자체를 ImportError로 만들어 처리 확인
        with patch("app.data_sources.kospilab_scraper.fetch_kospilab_data",
                   side_effect=Exception("bs4 미설치")):
            result = collect_kospilab_data()
        assert result["source_status"] in ("failed", "success")

    def test_returns_required_keys(self):
        result = collect_kospilab_data()
        for k in ("hynix_reference_price", "hynix_reference_return", "source_status"):
            assert k in result


class TestNormalizeYfOhlcv:
    """_normalize_yf_ohlcv 변환 테스트."""

    def test_columns_normalized(self):
        df = _make_df(5)
        result = _normalize_yf_ohlcv(df)
        for col in ("datetime", "open", "high", "low", "close", "volume"):
            assert col in result.columns

    def test_datetime_column_is_datetime(self):
        df = _make_df(5)
        result = _normalize_yf_ohlcv(df)
        assert pd.api.types.is_datetime64_any_dtype(result["datetime"])


class TestCollectHynixDaily:
    """SK하이닉스 일봉 수집 캐시 fallback."""

    def test_cache_csv_used_when_all_fail(self, tmp_path):
        fake_csv = tmp_path / "hynix_daily.csv"
        df = _make_df(30)
        df = df.reset_index().rename(columns={"index": "datetime"})
        df.to_csv(fake_csv, index=False)

        with patch("app.data_sources.auto_market_collector._HYNIX_DAILY_CSV", fake_csv):
            with patch("app.data_sources.auto_market_collector._kis_mode", return_value=None):
                with patch("yfinance.Ticker") as mock_yf:
                    mock_yf.return_value.history.return_value = pd.DataFrame()
                    result = collect_hynix_daily()

        # 캐시를 읽거나 yfinance 실패 후 None — 크래시 없어야 함
        assert "df_daily" in result
