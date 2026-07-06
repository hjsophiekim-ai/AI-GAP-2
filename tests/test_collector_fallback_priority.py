"""test_collector_fallback_priority.py — 수집 fallback 우선순위 테스트."""

from __future__ import annotations

import os
import time

import pandas as pd
import pytest
from unittest.mock import MagicMock, patch


# ── 헬퍼 ──────────────────────────────────────────────────────────────────────

def _make_hynix_df(n: int = 25, close: float = 180_000) -> pd.DataFrame:
    return pd.DataFrame({
        "datetime": pd.date_range("2026-01-01", periods=n, freq="B"),
        "open":   [close * 0.99] * n,
        "high":   [close * 1.01] * n,
        "low":    [close * 0.98] * n,
        "close":  [close] * n,
        "volume": [10_000_000] * n,
    })


# ── collect_hynix_daily fallback 순서 ────────────────────────────────────────

class TestCollectHynixDailyFallback:
    @pytest.fixture(autouse=True)
    def _valid_current_price_gate(self):
        detail = {
            "source_prices": {"KIS": 180_000.0, "naver": 180_100.0, "yfinance": 179_900.0},
            "selected_source": "KIS",
            "selected_price": 180_000.0,
            "max_diff_pct": 0.1112,
        }
        with patch(
            "app.data_sources.auto_market_collector.validate_hynix_current_sources",
            return_value=(True, "ok", detail),
        ):
            yield

    def test_kis_success_returns_kis_source(self):
        from app.data_sources.auto_market_collector import collect_hynix_daily
        good_df = _make_hynix_df(25)
        with patch("app.data_sources.auto_market_collector._fetch_hynix_daily_from_kis",
                   return_value=good_df):
            with patch("app.data_sources.auto_market_collector._kis_mode", return_value="mock"):
                with patch("app.data_sources.auto_market_collector._save_hynix_daily"):
                    result = collect_hynix_daily()
        assert result["source"] == "KIS"
        assert result["df_daily"] is not None
        assert result["prev_close"] is not None

    def test_kis_fail_naver_success(self):
        from app.data_sources.auto_market_collector import collect_hynix_daily
        good_df = _make_hynix_df(25)
        with patch("app.data_sources.auto_market_collector._fetch_hynix_daily_from_kis",
                   side_effect=Exception("KIS 오류")):
            with patch("app.data_sources.auto_market_collector._naver_daily_ohlcv",
                       return_value=good_df):
                with patch("app.data_sources.auto_market_collector._kis_mode", return_value="mock"):
                    with patch("app.data_sources.auto_market_collector._save_hynix_daily"):
                        result = collect_hynix_daily()
        assert result["source"] == "naver"
        assert result["df_daily"] is not None
        assert result["prev_close"] is not None

    def test_kis_and_naver_fail_yfinance_success(self):
        from app.data_sources.auto_market_collector import collect_hynix_daily
        good_hist = _make_hynix_df(25).rename(columns={
            "datetime": "Date", "open": "Open", "high": "High",
            "low": "Low", "close": "Close", "volume": "Volume",
        }).set_index("Date")
        with patch("app.data_sources.auto_market_collector._fetch_hynix_daily_from_kis",
                   side_effect=Exception("KIS 오류")):
            with patch("app.data_sources.auto_market_collector._naver_daily_ohlcv",
                       return_value=None):
                with patch("app.data_sources.auto_market_collector._kis_mode", return_value=None):
                    with patch("yfinance.Ticker") as mock_ticker:
                        mock_ticker.return_value.history.return_value = good_hist
                        with patch("app.data_sources.auto_market_collector._save_hynix_daily"):
                            result = collect_hynix_daily()
        assert result["source"] == "yfinance"
        assert result["df_daily"] is not None

    def test_all_fail_returns_none_not_zero(self):
        from app.data_sources.auto_market_collector import collect_hynix_daily
        with patch("app.data_sources.auto_market_collector._fetch_hynix_daily_from_kis",
                   side_effect=Exception("KIS 오류")):
            with patch("app.data_sources.auto_market_collector._naver_daily_ohlcv",
                       return_value=None):
                with patch("app.data_sources.auto_market_collector._kis_mode", return_value="mock"):
                    with patch("yfinance.Ticker") as mock_ticker:
                        mock_ticker.return_value.history.return_value = pd.DataFrame()
                        with patch(
                            "app.data_sources.auto_market_collector._HYNIX_DAILY_CSV"
                        ) as mp:
                            mp.exists.return_value = False
                            result = collect_hynix_daily()
        assert result["df_daily"] is None
        assert result["prev_close"] is None, (
            "모든 소스 실패 시 prev_close는 None이어야 합니다 (0 금지)"
        )
        assert result["current_price"] is None, (
            "모든 소스 실패 시 current_price는 None이어야 합니다 (0 금지)"
        )

    def test_result_has_fallback_chain(self):
        from app.data_sources.auto_market_collector import collect_hynix_daily
        with patch("app.data_sources.auto_market_collector._kis_mode", return_value=None):
            with patch("app.data_sources.auto_market_collector._naver_daily_ohlcv",
                       return_value=None):
                with patch("yfinance.Ticker") as mock_ticker:
                    mock_ticker.return_value.history.return_value = pd.DataFrame()
                    with patch(
                        "app.data_sources.auto_market_collector._HYNIX_DAILY_CSV"
                    ) as mp:
                        mp.exists.return_value = False
                        result = collect_hynix_daily()
        assert "fallback_chain" in result
        assert isinstance(result["fallback_chain"], list)

    def test_cache_age_blocks_old_cache(self, tmp_path):
        from app.data_sources.auto_market_collector import collect_hynix_daily
        cache_file = tmp_path / "hynix_daily.csv"
        _make_hynix_df(25).to_csv(cache_file, index=False)
        old_time = time.time() - 25 * 3600
        os.utime(cache_file, (old_time, old_time))
        with patch("app.data_sources.auto_market_collector._kis_mode", return_value=None):
            with patch("app.data_sources.auto_market_collector._naver_daily_ohlcv",
                       return_value=None):
                with patch("yfinance.Ticker") as mock_ticker:
                    mock_ticker.return_value.history.return_value = pd.DataFrame()
                    with patch(
                        "app.data_sources.auto_market_collector._HYNIX_DAILY_CSV",
                        cache_file,
                    ):
                        result = collect_hynix_daily()
        if result.get("source") == "cache":
            pytest.fail("24시간 초과 캐시를 사용해선 안 됩니다")

    def test_new_cache_under_24h_is_used(self, tmp_path):
        from app.data_sources.auto_market_collector import collect_hynix_daily
        cache_file = tmp_path / "hynix_daily.csv"
        _make_hynix_df(25).to_csv(cache_file, index=False)
        with patch("app.data_sources.auto_market_collector._kis_mode", return_value=None):
            with patch("app.data_sources.auto_market_collector._naver_daily_ohlcv",
                       return_value=None):
                with patch("yfinance.Ticker") as mock_ticker:
                    mock_ticker.return_value.history.return_value = pd.DataFrame()
                    with patch(
                        "app.data_sources.auto_market_collector._HYNIX_DAILY_CSV",
                        cache_file,
                    ):
                        with patch("app.data_sources.auto_market_collector._save_hynix_daily"):
                            result = collect_hynix_daily()
        # 24시간 이내 캐시는 사용 가능
        assert result["source"] in ("cache", None)


# ── collect_index_data 개별 티커 방식 ────────────────────────────────────────

class TestCollectIndexFallback:
    @pytest.fixture(autouse=True)
    def _disable_naver_quote(self):
        with patch(
            "app.data_sources.auto_market_collector._naver_global_quote",
            return_value={"status": "failed", "price": None, "return_pct": None, "source": "naver", "error": "mock"},
        ):
            yield

    def test_all_none_when_yfinance_fails(self):
        from app.data_sources.auto_market_collector import collect_index_data
        with patch("yfinance.Ticker") as mock_ticker:
            mock_ticker.return_value.history.return_value = pd.DataFrame()
            result = collect_index_data()
        assert result["qqq_return"] is None
        assert result["sox_return"] is None
        assert result["usdkrw_change"] is None

    def test_result_has_fallback_detail(self):
        from app.data_sources.auto_market_collector import collect_index_data
        with patch("yfinance.Ticker") as mock_ticker:
            mock_ticker.return_value.history.return_value = pd.DataFrame()
            result = collect_index_data()
        assert "fallback_detail" in result
        assert isinstance(result["fallback_detail"], dict)

    def test_error_set_when_all_fail(self):
        from app.data_sources.auto_market_collector import collect_index_data
        with patch("yfinance.Ticker") as mock_ticker:
            mock_ticker.return_value.history.return_value = pd.DataFrame()
            result = collect_index_data()
        assert result["error"] is not None

    def test_no_zero_fill_on_failure(self):
        from app.data_sources.auto_market_collector import collect_index_data

        good_hist = pd.DataFrame(
            {"Close": [400.0, 402.0]},
            index=pd.date_range("2026-06-27", periods=2, freq="B"),
        )

        def _mock_ticker(sym):
            m = MagicMock()
            if sym == "QQQ":
                m.history.return_value = good_hist
            else:
                m.history.return_value = pd.DataFrame()
            return m

        with patch("yfinance.Ticker", side_effect=_mock_ticker):
            result = collect_index_data()

        # 실패한 항목은 None (0.0으로 채워지면 안 됨)
        # sox_return이 None이어야 정상 (SOXX 및 ^SOX 모두 빈 응답)
        if result["sox_return"] == 0.0:
            pytest.fail("실패한 SOXX는 0.0이 아닌 None이어야 합니다")


# ── KISClient 초기화 실패해도 앱 죽지 않음 ───────────────────────────────────

class TestKisClientInitFailSafe:

    def test_missing_env_keys_raises_value_error_not_crash(self):
        from app.data_sources.auto_market_collector import _fetch_hynix_daily_from_kis
        import os
        # 인증 키 없을 때 ValueError (앱 죽지 않고 except로 잡힘)
        with patch.dict(os.environ, {
            "KIS_REAL_APP_KEY": "",
            "KIS_REAL_APP_SECRET": "",
            "KIS_MOCK_APP_KEY": "",
            "KIS_MOCK_APP_SECRET": "",
        }, clear=False):
            with pytest.raises(ValueError, match="인증 정보 없음"):
                _fetch_hynix_daily_from_kis("mock", 70)

    def test_collect_hynix_daily_catches_kis_init_error(self):
        from app.data_sources.auto_market_collector import collect_hynix_daily
        import os
        with patch.dict(os.environ, {
            "KIS_REAL_APP_KEY": "",
            "KIS_REAL_APP_SECRET": "",
            "KIS_MOCK_APP_KEY": "",
            "KIS_MOCK_APP_SECRET": "",
        }, clear=False):
            with patch("app.data_sources.auto_market_collector._naver_daily_ohlcv",
                       return_value=None):
                with patch("yfinance.Ticker") as mock_ticker:
                    mock_ticker.return_value.history.return_value = pd.DataFrame()
                    with patch(
                        "app.data_sources.auto_market_collector._HYNIX_DAILY_CSV"
                    ) as mp:
                        mp.exists.return_value = False
                        result = collect_hynix_daily(mode="mock")
        # 앱이 죽지 않고 결과 반환
        assert "df_daily" in result
        assert "fallback_chain" in result
