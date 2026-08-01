"""test_mu_extended_hours.py — MU 장외(프리마켓/애프터마켓) 데이터 수집/반영 검증(8개 시나리오)."""

from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from app.data_sources.kis_overseas_minute import (
    classify_current_session_type,
    classify_session,
    fetch_mu_3min_bars,
    fetch_mu_5min_bars,
)
from app.data_sources.mu_extended_hours_collector import collect_mu_extended_hours
from app.models.hynix_price_predictor import HynixPricePredictor

_TECH = {
    "rsi_14": 55.0, "macd_signal_cross": 1, "ma5_position_pct": 0.5, "return_3d_pct": 1.0,
    "volume_change_pct": 10.0, "from_20d_high_pct": -3.0, "ma20_position_pct": 1.0,
}


def _hynix_minute_df(base_price=250_000.0):
    return pd.DataFrame({"open": [base_price] * 10, "high": [base_price * 1.003] * 10,
                          "low": [base_price * 0.997] * 10, "close": [base_price] * 10, "volume": [10_000] * 10})


def _mu_market_data(extended_hours: dict | None = None) -> dict:
    return {
        "mu": {"source": "kis", "is_stale": False, "extended_hours": extended_hours},
        "nvda": {"source": "yahoo", "regular_return": 0.5}, "amd": {"source": "yahoo", "regular_return": 0.3},
        "avgo": {"source": "yahoo", "regular_return": 0.2},
        "index": {"source": "naver", "sox_return": 0.5, "qqq_return": 0.3, "usdkrw_change": 0.0},
        "domestic_index": {"source": "pykrx", "kospi_return": 0.2, "kospi200_return": 0.2},
        "investor_flow": {"source": "kis", "foreign_net_buy": 100_000, "institution_net_buy": 50_000},
        "hynix": {"source": "KIS"},
        "hynix_minute": {"source": "kis", "df_1min": _hynix_minute_df()},
    }


# 1. KIS MU 현재가 수집 성공 테스트
def test_kis_mu_current_price_success(monkeypatch):
    mock_resp = MagicMock()
    mock_resp.json.return_value = {"output": {"last": "984.75", "open": "980.0", "high": "990.0", "low": "975.0", "tvol": "1000000"}}
    mock_resp.raise_for_status = MagicMock()
    with patch("requests.get", return_value=mock_resp), \
         patch("app.data_sources.kis_overseas_minute._get_access_token", return_value="tok"), \
         patch("app.data_sources.kis_overseas_minute._load_credentials",
               return_value={"app_key": "k", "app_secret": "s", "base_url": "http://test"}):
        from app.data_sources.kis_overseas_minute import fetch_mu_current_price
        result = fetch_mu_current_price(mode="real")
    assert result is not None
    assert abs(result["price"] - 984.75) < 0.01  # 회귀 확인: /10으로 잘못 보정되지 않아야 함


# 2. KIS 실패 시 Alpaca fallback 테스트
def test_kis_failure_falls_back_to_alpaca(monkeypatch):
    monkeypatch.setattr("app.data_sources.mu_extended_hours_collector._fetch_via_kis", lambda mode: None)

    def _fake_quote(symbol):
        return {"price": 986.0, "change_pct": 0.5, "source": "alpaca", "success": True, "error": None, "timestamp": datetime.now().isoformat()}

    def _fake_bars(symbol, limit=120):
        df = pd.DataFrame({
            "datetime": pd.date_range("2026-07-07 05:00", periods=10, freq="1min"),
            "open": [986.0] * 10, "high": [987.0] * 10, "low": [985.0] * 10, "close": [986.0] * 10, "volume": [1000] * 10,
        })
        return df, "alpaca"

    monkeypatch.setattr("app.market.us_market_data.fetch_us_quote_multi", _fake_quote)
    monkeypatch.setattr("app.market.us_market_data.fetch_us_minute_bars_dataframe", _fake_bars)
    monkeypatch.setattr("app.market.us_market_data.fetch_us_last_session",
                         lambda symbol: {"success": True, "close": 984.0})

    result = collect_mu_extended_hours(mode=None)
    assert result["data_source"] == "alpaca"
    assert result["is_realtime"] is True
    assert result["is_delayed"] is False


# 3. 체결추이에서 1분봉/3분봉(+5분봉) 생성 테스트
def test_1min_to_3min_5min_bar_generation():
    minutes = pd.date_range("2026-07-07 05:00", periods=15, freq="1min")
    df_1min = pd.DataFrame({
        "datetime": minutes, "open": [100.0 + i for i in range(15)], "high": [101.0 + i for i in range(15)],
        "low": [99.0 + i for i in range(15)], "close": [100.5 + i for i in range(15)], "volume": [1000] * 15,
        "session": ["premarket"] * 15,
    })
    df_3min = fetch_mu_3min_bars(source_df=df_1min)
    df_5min = fetch_mu_5min_bars(source_df=df_1min)
    assert df_3min is not None and len(df_3min) == 5
    assert df_5min is not None and len(df_5min) == 3
    # 3분봉 volume은 1분봉 3개 합이어야 한다.
    assert df_3min.iloc[0]["volume"] == 3000


# 4. 프리마켓/애프터마켓 세션 구분 테스트
def test_premarket_afterhours_session_classification():
    premarket_ts = datetime(2026, 7, 7, 18, 0)   # 18:00 KST -> premarket band
    afterhours_ts = datetime(2026, 7, 7, 6, 0)   # 06:00 KST -> aftermarket band
    assert classify_session(premarket_ts) == "premarket"
    assert classify_session(afterhours_ts) == "aftermarket"


# 5. Yahoo fallback이면 confidence 상한 적용 테스트
def test_yahoo_fallback_caps_confidence():
    extended_delayed = {
        "session_type": "AFTERHOURS", "current_price": 986.0, "mu_extended_hours_score": 70.0,
        "data_source": "yahoo", "is_realtime": False, "is_delayed": True, "freshness_seconds": 120,
        "confidence_penalty_reason": ["'yahoo' 최후 보조 소스 사용"],
    }
    result = HynixPricePredictor().predict(
        _mu_market_data(extended_delayed), hynix_current_price=250_000, hynix_prev_close=250_000,
        tech_indicators=_TECH, micron_features={"micron_regular_return": 1.0}, now_hm="10:00",
    )
    assert result["confidence_tomorrow_open"] <= 60.0
    assert result["mu_extended_hours_auto_trade_usable"] is False


# 6. MU 장외 강세 시 하이닉스 내일시가 예측이 상향된다
def test_strong_mu_extended_hours_raises_tomorrow_open():
    strong = {
        "session_type": "PREMARKET", "current_price": 1000.0, "mu_extended_hours_score": 85.0,
        "data_source": "kis", "is_realtime": True, "is_delayed": False, "freshness_seconds": 30,
        "confidence_penalty_reason": [],
    }
    weak = {**strong, "mu_extended_hours_score": 15.0}

    r_strong = HynixPricePredictor().predict(
        _mu_market_data(strong), hynix_current_price=250_000, hynix_prev_close=250_000,
        tech_indicators=_TECH, micron_features={"micron_regular_return": 1.0}, now_hm="10:00",
    )
    r_weak = HynixPricePredictor().predict(
        _mu_market_data(weak), hynix_current_price=250_000, hynix_prev_close=250_000,
        tech_indicators=_TECH, micron_features={"micron_regular_return": 1.0}, now_hm="10:00",
    )
    assert r_strong["expected_return_pct_tomorrow_open"] > r_weak["expected_return_pct_tomorrow_open"]


# 7. MU 장외 약세 시 하이닉스 내일시가 예측이 하향된다(6번과 동일 비교, 관점만 반대)
def test_weak_mu_extended_hours_lowers_tomorrow_open():
    neutral_high = {
        "session_type": "AFTERHOURS", "current_price": 1000.0, "mu_extended_hours_score": 80.0,
        "data_source": "alpaca", "is_realtime": True, "is_delayed": False, "freshness_seconds": 20,
        "confidence_penalty_reason": [],
    }
    weak = {**neutral_high, "mu_extended_hours_score": 10.0}

    r_high = HynixPricePredictor().predict(
        _mu_market_data(neutral_high), hynix_current_price=250_000, hynix_prev_close=250_000,
        tech_indicators=_TECH, micron_features={"micron_regular_return": 0.0}, now_hm="10:00",
    )
    r_weak = HynixPricePredictor().predict(
        _mu_market_data(weak), hynix_current_price=250_000, hynix_prev_close=250_000,
        tech_indicators=_TECH, micron_features={"micron_regular_return": 0.0}, now_hm="10:00",
    )
    assert r_weak["expected_return_pct_tomorrow_open"] < r_high["expected_return_pct_tomorrow_open"]


# 8. 장외 데이터 없음 시 자동매수(자동매매 참고) 금지 테스트
def test_no_extended_hours_data_blocks_auto_trade_usage():
    result = HynixPricePredictor().predict(
        _mu_market_data(extended_hours=None), hynix_current_price=250_000, hynix_prev_close=250_000,
        tech_indicators=_TECH, micron_features={"micron_regular_return": 0.5}, now_hm="10:00",
    )
    assert result["mu_extended_hours_auto_trade_usable"] is False
    assert result["confidence_tomorrow_open"] <= 55.0
