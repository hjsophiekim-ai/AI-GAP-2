"""test_hynix_price_predictor.py — 다중 horizon(30분/1시간/3시간/오늘종가/내일시가)
SK하이닉스 가격 예측기 테스트.
"""

from __future__ import annotations

import json

import pandas as pd
import pytest

from app.models.hynix_price_predictor import (
    HORIZONS,
    HynixPricePredictor,
    _direction_probabilities,
    predict_hynix_multi_horizon,
)


def _hynix_minute_df(base_price: float = 250_000.0) -> pd.DataFrame:
    return pd.DataFrame({
        "open": [base_price] * 10,
        "high": [base_price * 1.004] * 10,
        "low": [base_price * 0.996] * 10,
        "close": [base_price * 1.001] * 10,
        "volume": [10_000] * 10,
    })


def _full_market_data(**overrides) -> dict:
    data = {
        "mu": {"source": "alpaca", "is_stale": False},
        "nvda": {"source": "yahoo", "regular_return": 2.1},
        "amd": {"source": "yahoo", "regular_return": 1.5},
        "avgo": {"source": "yahoo", "regular_return": 0.8},
        "index": {"source": "naver", "sox_return": 3.2, "qqq_return": 1.1, "usdkrw_change": -0.3},
        "domestic_index": {"source": "pykrx", "kospi_return": 0.5, "kospi200_return": 0.6},
        "investor_flow": {"source": "kis", "foreign_net_buy": 500_000, "institution_net_buy": 100_000},
        "hynix": {"source": "KIS"},
        "hynix_minute": {"source": "kis", "df_1min": _hynix_minute_df()},
    }
    data.update(overrides)
    return data


_TECH = {
    "rsi_14": 62.0, "macd_signal_cross": 1, "ma5_position_pct": 2.3, "return_3d_pct": 4.1,
    "volume_change_pct": 15.0, "from_20d_high_pct": -3.0, "ma20_position_pct": 5.0,
}
_MICRON = {"micron_regular_return": 4.5}


def test_full_data_produces_all_horizons():
    """모든 데이터가 있으면 5개 horizon 전부 가격/신뢰도/확률이 채워진다."""
    r = predict_hynix_multi_horizon(
        _full_market_data(), hynix_current_price=250_500, hynix_prev_close=245_000,
        tech_indicators=_TECH, micron_features=_MICRON,
    )
    for horizon in HORIZONS:
        price_key = {"close": "predicted_close_today", "tomorrow_open": "predicted_open_tomorrow"}.get(
            horizon, f"predicted_price_{horizon}"
        )
        assert r[price_key] is not None
        assert r[f"confidence_{horizon}"] > 0
        assert r[f"probability_up_{horizon}"] + r[f"probability_sideways_{horizon}"] + r[f"probability_down_{horizon}"] == pytest.approx(100.0, abs=0.2)
    assert r["data_quality_score"] > 0
    assert isinstance(r["key_reasons"], list) and len(r["key_reasons"]) > 0


def test_missing_all_us_data_does_not_crash_and_lowers_quality():
    """미국 데이터가 전부 없어도 예외 없이 낮은 데이터품질/경고로 처리된다."""
    md = _full_market_data(mu={"source": None, "is_stale": False}, nvda={}, amd={}, avgo={}, index={})
    r = predict_hynix_multi_horizon(md, hynix_current_price=250_500, hynix_prev_close=245_000,
                                     tech_indicators=_TECH, micron_features={})
    assert r["predicted_close_today"] is not None
    assert r["data_quality_score"] < 100
    assert any("미국 반도체" in w for w in r["missing_data_warning"])


def test_holiday_mode_caps_confidence_at_85(monkeypatch):
    import app.models.hynix_price_predictor as mod

    monkeypatch.setattr(
        "app.market.us_market_data.get_us_market_status",
        lambda: {"is_us_holiday": True, "is_us_weekend": False},
    )
    r = predict_hynix_multi_horizon(
        _full_market_data(), hynix_current_price=250_500, hynix_prev_close=245_000,
        tech_indicators=_TECH, micron_features=_MICRON,
    )
    assert r["holiday_mode"] is True
    assert r["data_quality_score"] <= 85.0
    for horizon in HORIZONS:
        assert r[f"confidence_{horizon}"] <= 85.0


def test_mu_relative_strength_vs_sox_computed():
    predictor = HynixPricePredictor()
    stage = predictor._stage_us_ai_semi(
        _full_market_data(), {"micron_regular_return": 5.0},
    )
    # index.sox_return=3.2 (fixture) -> 5.0 - 3.2 = 1.8
    assert stage["mu_relative_strength_vs_sox"] == pytest.approx(1.8, abs=1e-6)


def test_mu_stale_reduces_confidence(monkeypatch):
    monkeypatch.setattr(
        "app.market.us_market_data.get_us_market_status",
        lambda: {"is_us_holiday": False, "is_us_weekend": False},
    )
    fresh = predict_hynix_multi_horizon(
        _full_market_data(mu={"source": "alpaca", "is_stale": False}),
        hynix_current_price=250_500, hynix_prev_close=245_000,
        tech_indicators=_TECH, micron_features=_MICRON,
    )
    stale = predict_hynix_multi_horizon(
        _full_market_data(mu={"source": "alpaca", "is_stale": True}),
        hynix_current_price=250_500, hynix_prev_close=245_000,
        tech_indicators=_TECH, micron_features=_MICRON,
    )
    assert stale["data_quality_score"] < fresh["data_quality_score"]
    assert any("stale" in w or "지연" in w for w in stale["missing_data_warning"])


def test_investor_flow_cache_source_reduces_quality_with_warning(monkeypatch):
    monkeypatch.setattr(
        "app.market.us_market_data.get_us_market_status",
        lambda: {"is_us_holiday": False, "is_us_weekend": False},
    )
    live = predict_hynix_multi_horizon(
        _full_market_data(), hynix_current_price=250_500, hynix_prev_close=245_000,
        tech_indicators=_TECH, micron_features=_MICRON,
    )
    cached = predict_hynix_multi_horizon(
        _full_market_data(investor_flow={"source": "cache", "foreign_net_buy": 500_000, "institution_net_buy": 100_000}),
        hynix_current_price=250_500, hynix_prev_close=245_000,
        tech_indicators=_TECH, micron_features=_MICRON,
    )
    assert cached["data_quality_score"] < live["data_quality_score"]
    assert any("캐시" in w for w in cached["missing_data_warning"])


def test_extreme_mu_move_widens_clip_only_for_close_and_tomorrow():
    r = predict_hynix_multi_horizon(
        _full_market_data(), hynix_current_price=250_500, hynix_prev_close=245_000,
        tech_indicators=_TECH, micron_features={"micron_regular_return": 12.0},
    )
    assert r["extreme_event"] is True
    # 30m/1h는 확장 클립 대상이 아니므로 절대 ±1.5%/±3.0%를 넘지 않는다.
    assert abs(r["expected_return_pct_30m"]) <= 1.5 + 1e-6
    assert abs(r["expected_return_pct_1h"]) <= 3.0 + 1e-6
    # close/tomorrow_open은 확장 클립(1.4배)까지 허용되므로 기본 클립보다 클 수 있다.
    assert abs(r["expected_return_pct_close"]) <= 7.0 * 1.4 + 1e-6
    assert abs(r["expected_return_pct_tomorrow_open"]) <= 7.0 * 1.4 + 1e-6


def test_sanity_clip_prevents_unrealistic_price_gap():
    """비정상적으로 큰 신호가 들어와도 기준가 대비 사전에 정의한 clip 이상 벗어나지 않는다."""
    extreme_tech = dict(_TECH, return_3d_pct=50.0, ma5_position_pct=40.0, volume_change_pct=500.0)
    r = predict_hynix_multi_horizon(
        _full_market_data(), hynix_current_price=2_500_000, hynix_prev_close=2_400_000,
        tech_indicators=extreme_tech, micron_features={"micron_regular_return": 15.0},
    )
    base = r["base_price"]
    for horizon, clip in [("30m", 1.5), ("1h", 3.0), ("3h", 5.0)]:
        price_key = f"predicted_price_{horizon}"
        price = r[price_key]
        gap_pct = abs(price - base) / base * 100
        assert gap_pct <= clip + 0.1, f"{horizon} gap {gap_pct} exceeds clip {clip}"


def test_no_base_price_returns_none_without_crash():
    r = predict_hynix_multi_horizon(
        _full_market_data(), hynix_current_price=None, hynix_prev_close=None,
        tech_indicators=_TECH, micron_features=_MICRON,
    )
    assert r["base_price"] is None
    assert r["predicted_close_today"] is None
    assert r["predicted_open_tomorrow"] is None
    assert r["predicted_price_30m"] is None


def test_direction_probabilities_never_reach_absolute_certainty():
    for value in (-100.0, -1.0, 0.0, 1.0, 100.0):
        p_up, p_side, p_down = _direction_probabilities(value, sideways_pct=0.5)
        assert 0.0 < p_up < 100.0
        assert 0.0 < p_down < 100.0
        assert p_up + p_side + p_down == pytest.approx(100.0, abs=0.2)


def test_prediction_is_logged_to_jsonl(tmp_path, monkeypatch):
    import app.models.hynix_price_predictor as mod

    monkeypatch.setattr(mod, "LOG_DIR", tmp_path / "hynix_prediction")
    r = predict_hynix_multi_horizon(
        _full_market_data(), hynix_current_price=250_500, hynix_prev_close=245_000,
        tech_indicators=_TECH, micron_features=_MICRON,
    )
    files = list((tmp_path / "hynix_prediction").glob("*.jsonl"))
    assert len(files) == 1
    lines = files[0].read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["predicted_close_today"] == r["predicted_close_today"]
    assert record["data_quality_score"] == r["data_quality_score"]


def test_forecast_engine_attaches_price_prediction_without_breaking_existing_result():
    """hynix_forecast_engine.run_forecast()가 기존 필드를 깨지 않고 price_prediction을 추가한다."""
    from datetime import datetime, timedelta
    from app.ml.hynix_forecast_engine import run_forecast

    def _daily_df(n=70):
        rows = []
        price = 195_000.0
        for i in range(n):
            price = price * (1.0 + 0.001 * (i % 5 - 2))
            rows.append({
                "datetime": datetime(2026, 1, 1) + timedelta(days=i),
                "open": price * 0.998, "high": price * 1.008, "low": price * 0.992,
                "close": price, "volume": 5_000_000 + i * 10_000,
            })
        return pd.DataFrame(rows)

    mu_df = pd.DataFrame({
        "datetime": pd.date_range("2026-06-29 17:00", periods=60, freq="1min"),
        "open": [101.5 + i * 0.01 for i in range(60)],
        "high": [103.0 + i * 0.01 for i in range(60)],
        "low": [100.5 + i * 0.01 for i in range(60)],
        "close": [102.0 + i * 0.01 for i in range(60)],
        "volume": [80_000 + i for i in range(60)],
        "session": ["premarket"] * 60,
    })
    mu_daily = pd.DataFrame({
        "datetime": pd.date_range("2026-05-01", periods=30, freq="B"),
        "open": [100 + i * 0.1 for i in range(30)], "high": [101 + i * 0.1 for i in range(30)],
        "low": [99 + i * 0.1 for i in range(30)], "close": [100 + i * 0.1 for i in range(30)],
        "volume": [1_000_000 + i for i in range(30)],
    })
    market_data = {
        "mu": {
            "df_1min": mu_df, "df_3min": mu_df.iloc[::3].reset_index(drop=True), "df_daily": mu_daily,
            "current_price": {"price": 102.0, "open": 101.5, "high": 103.0, "low": 100.5},
            "source": "yfinance", "error": None,
        },
        "nvda": {"current_price": 120.0, "premarket_return": None, "regular_return": 1.8, "source": "yfinance", "error": None},
        "index": {"qqq_return": 1.0, "sox_return": 1.5, "usdkrw_change": 0.2, "source": "yfinance", "error": None},
        "hynix": {
            "df_daily": _daily_df(), "prev_close": 195_000.0, "current_price": 195_500.0, "source": "yfinance",
            "stock_identity": {"code": "000660", "name": "SK하이닉스", "ok": True, "message": "ok"},
            "price_validation": {"ok": True, "message": "ok", "source_prices": {}, "selected_source": "KIS", "selected_price": 195_500.0},
            "error": None,
        },
        "kospilab": {
            "hynix_reference_price": 196_000.0, "hynix_reference_return": 0.5,
            "source_status": "success", "error_message": None,
        },
        "errors": [],
    }

    result = run_forecast(market_data)
    assert "price_prediction" in result
    assert result["prediction"] is not None  # 기존 필드는 그대로 채워진다
    if result["price_prediction"] is not None:
        assert result["price_prediction"]["current_price"] == 195_500.0
