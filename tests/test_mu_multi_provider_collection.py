"""
하이닉스 예측모듈 - 마이크론(MU) 다중소스 분봉/시세 수집 보강 테스트.

검증 항목:
  1. Alpaca 분봉이 있으면 최우선으로 사용된다.
  2. Alpaca 키가 없으면 Polygon으로, 그것도 없으면 기존 yfinance 경로로 넘어간다.
  3. Finnhub은 시세(quote) 우선순위에서만 사용된다(분봉 아님).
  4. 휴장일에 분봉이 없는 것은 오류(API_FAILURE)가 아니라 정상(data_gap_reason)으로 분류된다.
  5. 개장일에 데이터가 15분 이상 stale이면 경고 로그 + is_stale=True로 표시된다.
  6. is_stale이 hynix_auto_features의 data_quality에 반영된다.
  7. 최종 실패 시 last_session(마지막 거래일) 정보를 확보한다.
"""

from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from app.data_sources import auto_market_collector as amc
from app.market import us_market_data as umd


def _make_1min_df(n=30, start="2026-06-29 17:00"):
    return pd.DataFrame({
        "datetime": pd.date_range(start, periods=n, freq="1min", tz="UTC"),
        "open": [100.0 + i * 0.02 for i in range(n)],
        "high": [101.0 + i * 0.02 for i in range(n)],
        "low": [99.0 + i * 0.02 for i in range(n)],
        "close": [100.5 + i * 0.02 for i in range(n)],
        "volume": [50_000 + i for i in range(n)],
    })


# ---------------------------------------------------------------------------
# 1/2. 우선순위: Alpaca -> Polygon -> yfinance
# ---------------------------------------------------------------------------

def test_alpaca_bars_used_when_available():
    alpaca_bars = [
        {"open": 100.0 + i, "high": 101.0 + i, "low": 99.0 + i, "close": 100.5 + i,
         "volume": 1000 + i, "time": f"2026-07-01T14:{i:02d}:00Z"}
        for i in range(15)
    ]
    with patch.object(umd, "_fetch_alpaca_bars", return_value=alpaca_bars), \
         patch.object(umd, "_fetch_polygon_bars", return_value=[]):
        df = amc._fetch_multi_provider_intraday("MU", limit=15)

    assert df is not None
    assert (df["source"] == "alpaca").all()
    assert len(df) == 15


def test_falls_back_to_polygon_when_alpaca_empty():
    polygon_bars = [
        {"open": 100.0 + i, "high": 101.0 + i, "low": 99.0 + i, "close": 100.5 + i,
         "volume": 1000 + i, "time": 1751370000000 + i * 60000}
        for i in range(12)
    ]
    with patch.object(umd, "_fetch_alpaca_bars", return_value=[]), \
         patch.object(umd, "_fetch_polygon_bars", return_value=polygon_bars):
        df = amc._fetch_multi_provider_intraday("MU", limit=12)

    assert df is not None
    assert (df["source"] == "polygon").all()


def test_falls_back_to_existing_yfinance_path_when_no_keys():
    """Alpaca/Polygon 모두 실패하면 기존에 검증된 _fetch_yfinance_intraday로 그대로 넘어간다."""
    yf_df = _make_1min_df()
    with patch.object(umd, "_fetch_alpaca_bars", return_value=[]), \
         patch.object(umd, "_fetch_polygon_bars", return_value=[]), \
         patch("app.data_sources.auto_market_collector._fetch_yfinance_intraday", return_value=yf_df) as mock_yf:
        df = amc._fetch_multi_provider_intraday("MU", limit=60)

    mock_yf.assert_called_once()
    assert df is not None
    assert "source" in df.columns
    assert (df["source"] == "yfinance").all()


def test_alpaca_keys_missing_does_not_raise():
    """API 키가 없으면 조용히 skip되고 예외를 던지지 않는다."""
    assert umd._fetch_alpaca_bars("MU") == []
    df = amc._fetch_multi_provider_intraday("MU", limit=10)  # 실제 네트워크가 없어도 예외 없이 반환
    assert df is None or hasattr(df, "empty")


# ---------------------------------------------------------------------------
# 3. Finnhub은 quote 전용
# ---------------------------------------------------------------------------

def test_finnhub_used_for_quote_only_not_bars():
    with patch.object(umd, "_fetch_alpaca_quote", return_value=None), \
         patch.object(umd, "_fetch_polygon_quote", return_value=None), \
         patch.object(umd, "_fetch_finnhub_quote", return_value={"price": 99.0, "prev_close": 97.0, "source": "finnhub"}):
        quote = amc._multi_provider_quote_then_naver_yfinance("MU")

    assert quote["source"] == "finnhub"
    assert quote["price"] == 99.0
    # Finnhub bars fetch function does not exist / is never used for bars.
    assert not hasattr(umd, "_fetch_finnhub_bars")


# ---------------------------------------------------------------------------
# 4. 휴장일 데이터 공백은 정상 처리
# ---------------------------------------------------------------------------

def test_holiday_missing_bars_is_not_api_failure():
    with patch("app.data_sources.auto_market_collector._kis_mode", return_value=None), \
         patch("app.data_sources.auto_market_collector._us_market_open_now", return_value=False), \
         patch("app.data_sources.auto_market_collector._fetch_multi_provider_intraday", return_value=None), \
         patch("app.data_sources.auto_market_collector._multi_provider_quote_then_naver_yfinance",
               return_value={"symbol": "MU", "price": None, "return_pct": None, "source": "failed", "status": "failed", "error": "x"}), \
         patch("app.data_sources.auto_market_collector._load_mu_1min_cache", return_value=None), \
         patch("app.data_sources.auto_market_collector._MU_1MIN_CSV") as mock_csv, \
         patch("app.data_sources.auto_market_collector._mu_holiday_gap_reason", return_value="US_HOLIDAY"):
        mock_csv.exists.return_value = False
        result = amc.collect_mu_data()

    assert result["data_gap_reason"] == "US_HOLIDAY"
    # 휴장 시 error 메시지에 "collection failed" 같은 알람성 문구를 추가하지 않는다
    assert result.get("error") is None or "collection failed" not in (result.get("error") or "")


def test_normal_trading_day_api_failure_is_flagged():
    with patch("app.data_sources.auto_market_collector._kis_mode", return_value=None), \
         patch("app.data_sources.auto_market_collector._us_market_open_now", return_value=True), \
         patch("app.data_sources.auto_market_collector._fetch_multi_provider_intraday", return_value=None), \
         patch("app.data_sources.auto_market_collector._multi_provider_quote_then_naver_yfinance",
               return_value={"symbol": "MU", "price": None, "return_pct": None, "source": "failed", "status": "failed", "error": "x"}), \
         patch("app.data_sources.auto_market_collector._load_mu_1min_cache", return_value=None), \
         patch("app.data_sources.auto_market_collector._MU_1MIN_CSV") as mock_csv:
        mock_csv.exists.return_value = False
        result = amc.collect_mu_data()

    assert result["data_gap_reason"] == "API_FAILURE"


# ---------------------------------------------------------------------------
# 5/6. 개장일 stale 판정 + data_quality 반영
# ---------------------------------------------------------------------------

def test_stale_data_during_market_hours_flagged_and_logged(caplog):
    import logging
    old_df = _make_1min_df(n=15, start="2020-01-01 09:00")  # 아주 오래된 타임스탬프
    with caplog.at_level(logging.WARNING, logger="app.data_sources.auto_market_collector"):
        is_stale = amc._check_mu_staleness(old_df, market_open=True)
    assert is_stale is True
    assert any("stale" in r.message for r in caplog.records)


def test_not_stale_when_market_closed_even_if_old():
    old_df = _make_1min_df(n=15, start="2020-01-01 09:00")
    assert amc._check_mu_staleness(old_df, market_open=False) is False


def test_hynix_data_quality_reduced_when_mu_stale():
    from app.features.hynix_auto_features import build_auto_features

    mu_df = _make_1min_df(n=90, start="2026-07-01 09:00")
    mu_df["session"] = "regular"
    market_data = {
        "mu": {"df_1min": mu_df, "current_price": {"price": 101.0, "open": 100.0, "high": 102.0, "low": 99.0}, "is_stale": True, "source": "yahoo"},
        "nvda": {"regular_return": 1.0, "source": "yahoo"},
        "index": {"sox_return": 1.0, "qqq_return": 0.5, "usdkrw_change": 0.1},
        "hynix": {"df_daily": None, "prev_close": 200000, "current_price": 201000, "source": "kis"},
        "kospilab": {"hynix_reference_return": 1.0, "hynix_reference_price": 205000, "source_status": "ok"},
    }
    fresh = build_auto_features({**market_data, "mu": {**market_data["mu"], "is_stale": False}})
    stale = build_auto_features(market_data)

    assert stale["mu_is_stale"] is True
    assert fresh["mu_is_stale"] is False
    assert stale["data_quality"] <= fresh["data_quality"]


# ---------------------------------------------------------------------------
# 7. 최종 실패 시 last_session 확보
# ---------------------------------------------------------------------------

def test_last_session_captured_on_total_failure():
    with patch("app.data_sources.auto_market_collector._kis_mode", return_value=None), \
         patch("app.data_sources.auto_market_collector._us_market_open_now", return_value=False), \
         patch("app.data_sources.auto_market_collector._fetch_multi_provider_intraday", return_value=None), \
         patch("app.data_sources.auto_market_collector._multi_provider_quote_then_naver_yfinance",
               return_value={"symbol": "MU", "price": None, "return_pct": None, "source": "failed", "status": "failed", "error": "x"}), \
         patch.object(umd, "fetch_us_last_session", return_value={"symbol": "MU", "close": 95.0, "change_rate": -1.2, "success": True, "source": "yahoo"}), \
         patch("app.data_sources.auto_market_collector._load_mu_1min_cache", return_value=None), \
         patch("app.data_sources.auto_market_collector._MU_1MIN_CSV") as mock_csv:
        mock_csv.exists.return_value = False
        result = amc.collect_mu_data()

    assert result["last_session"] is not None
    assert result["last_session"]["close"] == 95.0
