"""test_recovery_and_bias_calibration.py — recovery_score/관성편향 완화/rolling bias
calibration 검증 (12개 시나리오, 명세 순서대로).
"""

from __future__ import annotations

import json

from unittest.mock import MagicMock

import pandas as pd
import pytest

from app.execution.auto_trader import AutoTrader
from app.market import market_prediction as mp
from app.market import regime_features as rf
from app.models import model_calibration
from app.models.hynix_price_predictor import (
    HynixPricePredictor,
    _compute_confidence,
    _tomorrow_state,
    predict_hynix_multi_horizon,
)

_TECH = {
    "rsi_14": 40.0, "macd_signal_cross": -1, "ma5_position_pct": -1.5, "return_3d_pct": -3.0,
    "volume_change_pct": 20.0, "from_20d_high_pct": -8.0, "ma20_position_pct": -4.0,
}
_MICRON = {"micron_regular_return": -2.0}


def _hynix_minute_df(base_price: float) -> pd.DataFrame:
    return pd.DataFrame({
        "open": [base_price] * 10, "high": [base_price * 1.003] * 10,
        "low": [base_price * 0.997] * 10, "close": [base_price] * 10, "volume": [10_000] * 10,
    })


def _hynix_market_data(**overrides) -> dict:
    data = {
        "mu": {"source": "alpaca", "is_stale": False},
        "nvda": {"source": "yahoo", "regular_return": -1.0},
        "amd": {"source": "yahoo", "regular_return": -0.5},
        "avgo": {"source": "yahoo", "regular_return": -0.3},
        "index": {"source": "naver", "sox_return": -1.5, "qqq_return": -0.8, "usdkrw_change": 0.2},
        "domestic_index": {"source": "pykrx", "kospi_return": -0.5, "kospi200_return": -0.6},
        "investor_flow": {"source": "kis", "foreign_net_buy": -300_000, "institution_net_buy": -50_000},
        "hynix": {"source": "KIS"},
        "hynix_minute": {"source": "kis", "df_1min": _hynix_minute_df(250_000.0)},
    }
    data.update(overrides)
    return data


# 1. recovery_score가 높으면 D 유지 예측이 완화된다 (D -> C).
def test_high_recovery_score_softens_d_persistence():
    regime = _expected_regime_call(down_pressure=60.0, recovery_score=80.0, collapse_declining=True, current_regime="D")
    assert regime == "C"


def _expected_regime_call(down_pressure, recovery_score, collapse_declining, current_regime):
    return mp._expected_regime(
        direction="DOWN", down_pressure=down_pressure, market_collapse_score=70.0,
        current_regime=current_regime, recovery_score=recovery_score, collapse_declining=collapse_declining,
    )


# 2. collapse_score가 하락 중(완화)이면 3시간 DOWN 확률이 낮아진다.
def test_declining_collapse_score_lowers_3h_down_probability():
    snapshot = {"domestic": {"investor_flow_market": {"foreign_net_buy_sum": -2_000_000, "success": True}},
                "overseas": {"usdkrw": {"change_rate": 0.5, "success": True}}, "deltas": {}}
    regime_result = {"regime": "D", "scores": {"market_collapse_score": 75.0}}

    no_decline = mp.predict_market_direction("3h", snapshot, regime_result, score_deltas=None)
    declining = mp.predict_market_direction(
        "3h", snapshot, regime_result,
        recovery_info={"recovery_score": 80.0},
        score_deltas={"market_collapse_score_delta_15m": -20.0, "regime_transition_momentum": 15.0},
    )
    assert declining["probability_down"] < no_decline["probability_down"]


# 3. 하이닉스 VWAP 재돌파 + 선물 반등 시 예상가격이 상향 보정된다.
def test_vwap_reclaim_and_futures_rebound_raise_predicted_price():
    weak = predict_hynix_multi_horizon(
        _hynix_market_data(domestic_index={"source": "pykrx", "kospi_return": -1.0, "kospi200_return": -1.2},
                            investor_flow={"source": "kis", "foreign_net_buy": -800_000, "institution_net_buy": -200_000}),
        hynix_current_price=245_000, hynix_prev_close=250_000, tech_indicators=_TECH, micron_features=_MICRON,
    )
    recovering = predict_hynix_multi_horizon(
        _hynix_market_data(domestic_index={"source": "pykrx", "kospi_return": 0.8, "kospi200_return": 1.0},
                            investor_flow={"source": "kis", "foreign_net_buy": 900_000, "institution_net_buy": 300_000},
                            hynix_minute={"source": "kis", "df_1min": _hynix_minute_df(252_000.0)}),
        hynix_current_price=252_000, hynix_prev_close=250_000, tech_indicators=_TECH, micron_features=_MICRON,
    )
    assert recovering["recovery_score"] > weak["recovery_score"]
    assert recovering["expected_return_pct_3h"] > weak["expected_return_pct_3h"]


# 4. rolling bias가 "과거에 실제보다 낮게 예측했다"를 나타내면 예측가격이 상향 보정된다.
def test_rolling_bias_raises_predicted_price(tmp_path, monkeypatch):
    monkeypatch.setattr(model_calibration, "CALIBRATION_DIR", tmp_path)
    monkeypatch.setattr(model_calibration, "HYNIX_BIAS_PATH", tmp_path / "hynix_bias.json")

    without_bias = predict_hynix_multi_horizon(
        _hynix_market_data(), hynix_current_price=250_000, hynix_prev_close=250_000,
        tech_indicators=_TECH, micron_features=_MICRON,
    )

    model_calibration._write_json(model_calibration.HYNIX_BIAS_PATH, {
        "close": {"bias_pct": 0.6, "sample_count": 60, "computed_at": "2026-01-01T00:00:00"},
    })
    with_bias = predict_hynix_multi_horizon(
        _hynix_market_data(), hynix_current_price=250_000, hynix_prev_close=250_000,
        tech_indicators=_TECH, micron_features=_MICRON,
    )
    assert with_bias["predicted_close_today"] > without_bias["predicted_close_today"]


# 5. 표본 20개 미만이면 bias 보정이 30%만 반영된다.
def test_small_sample_bias_applies_only_30_percent(tmp_path, monkeypatch):
    monkeypatch.setattr(model_calibration, "CALIBRATION_DIR", tmp_path)
    monkeypatch.setattr(model_calibration, "HYNIX_BIAS_PATH", tmp_path / "hynix_bias.json")

    model_calibration._write_json(model_calibration.HYNIX_BIAS_PATH, {
        "close": {"bias_pct": 0.5, "sample_count": 10, "computed_at": "2026-01-01T00:00:00"},
    })
    small_sample_correction = model_calibration.get_hynix_bias_correction("close")
    assert small_sample_correction == pytest.approx(0.5 * 0.30, abs=1e-6)

    model_calibration._write_json(model_calibration.HYNIX_BIAS_PATH, {
        "close": {"bias_pct": 0.5, "sample_count": 60, "computed_at": "2026-01-01T00:00:00"},
    })
    large_sample_correction = model_calibration.get_hynix_bias_correction("close")
    assert large_sample_correction == pytest.approx(0.5 * 1.00, abs=1e-6)
    assert large_sample_correction > small_sample_correction


# 6. 핵심 데이터 3개 이상 부족하면 confidence 상한(60)이 적용된다.
def test_confidence_capped_when_core_data_missing():
    stage_results = {
        "us_ai_semi": {"used_weight": 0.0, "sources": {"mu": None}},
        "domestic_flow": {"used_weight": 0.0},
        "domestic_sector": {"used_weight": 0.0},
        "hynix_self": {"used_weight": 0.9},
    }
    confidence = _compute_confidence(
        horizon="close", data_quality_score=95.0, stage_results=stage_results, holiday_mode=False,
        mu_is_stale=False, hynix_source="KIS", investor_is_proxy=False, recovery_score=None,
        predicted_direction="UP", tomorrow_state=None,
    )
    assert confidence <= 60.0


# 7. 외국인 수급이 proxy이면 confidence 상한(75)이 적용된다.
def test_confidence_capped_when_investor_flow_is_proxy():
    stage_results = {
        "us_ai_semi": {"used_weight": 1.0, "sources": {"mu": "alpaca"}},
        "domestic_flow": {"used_weight": 1.0},
        "domestic_sector": {"used_weight": 1.0},
        "hynix_self": {"used_weight": 1.0},
    }
    confidence = _compute_confidence(
        horizon="close", data_quality_score=100.0, stage_results=stage_results, holiday_mode=False,
        mu_is_stale=False, hynix_source="KIS", investor_is_proxy=True, recovery_score=None,
        predicted_direction="UP", tomorrow_state=None,
    )
    assert confidence <= 75.0


# 8. Holiday Mode에서는 confidence 최대 85.
def test_confidence_capped_at_85_in_holiday_mode():
    stage_results = {
        "us_ai_semi": {"used_weight": 1.0, "sources": {"mu": "alpaca"}},
        "domestic_flow": {"used_weight": 1.0},
        "domestic_sector": {"used_weight": 1.0},
        "hynix_self": {"used_weight": 1.0},
    }
    confidence = _compute_confidence(
        horizon="close", data_quality_score=100.0, stage_results=stage_results, holiday_mode=True,
        mu_is_stale=False, hynix_source="KIS", investor_is_proxy=False, recovery_score=None,
        predicted_direction="UP", tomorrow_state=None,
    )
    assert confidence <= 85.0


# 9. 방향확률과 단일가격 방향이 항상 일치한다(설계상 동일 clipped_return에서 파생).
def test_direction_and_price_are_always_consistent():
    result = predict_hynix_multi_horizon(
        _hynix_market_data(), hynix_current_price=250_000, hynix_prev_close=255_000,
        tech_indicators=_TECH, micron_features=_MICRON,
    )
    for horizon in ("30m", "1h", "3h", "close", "tomorrow_open"):
        assert result[f"direction_price_consistent_{horizon}"] is True
        direction = result[f"direction_{horizon}"]
        p_up = result[f"probability_up_{horizon}"]
        p_down = result[f"probability_down_{horizon}"]
        if direction == "UP":
            assert p_up >= p_down
        elif direction == "DOWN":
            assert p_down >= p_up


# 10. 내일 시가 예측 상태가 4가지로 구분된다.
def test_tomorrow_state_classification():
    assert _tomorrow_state("07:30") == "US_SESSION_UPDATED"
    assert _tomorrow_state("08:55") == "PREOPEN_FINAL"
    assert _tomorrow_state("11:00") == "INTRADAY_PRELIMINARY"
    assert _tomorrow_state("16:00") == "CLOSING_BASED"


# 11. C->D뿐 아니라 D->C 회복 전환도 감지한다 (down_pressure가 35~55 구간으로 내려오면 C).
def test_d_to_c_recovery_transition_detected():
    regime = mp._expected_regime(
        direction="SIDEWAYS", down_pressure=42.0, market_collapse_score=60.0,
        current_regime="D", recovery_score=None, collapse_declining=False,
    )
    assert regime == "C"
    # 기존 C->D 전환(고위험)도 여전히 정상 동작해야 한다(회귀 확인).
    regime_cd = mp._expected_regime(
        direction="DOWN", down_pressure=75.0, market_collapse_score=85.0,
        current_regime="C", recovery_score=None, collapse_declining=False,
    )
    assert regime_cd == "E"


# 12. 하락편향 보정 후 MAPE가 보정 전보다 개선된다(합성 데이터: 항상 실제가 예측보다 높았던 이력).
def test_bias_correction_improves_mape(tmp_path, monkeypatch):
    monkeypatch.setattr(model_calibration, "CALIBRATION_DIR", tmp_path)
    monkeypatch.setattr(model_calibration, "HYNIX_BIAS_PATH", tmp_path / "hynix_bias.json")

    base = 250_000.0
    rows = []
    for i in range(60):
        predicted = base * (1 - 0.006)  # 항상 0.6% 낮게 예측
        actual = base  # 실제로는 변화 없음(체계적 하락 편향 시나리오)
        rows.append({"predicted_close_today": predicted, "actual_close_today": actual, "base_price": base})

    bias = model_calibration.compute_and_save_hynix_bias(rows)
    assert bias["close"]["bias_pct"] == pytest.approx(0.6, abs=0.01)
    assert bias["close"]["sample_count"] == 60

    correction = model_calibration.get_hynix_bias_correction("close")
    corrected_predicted = base * (1 - 0.006) * (1 + correction / 100)
    actual = base

    uncorrected_error = abs(actual - base * (1 - 0.006))
    corrected_error = abs(actual - corrected_predicted)
    assert corrected_error < uncorrected_error


# 보너스: D/E + recovery_score>=75 -> AUTO 매수는 금지, 수동승인만 허용(명세 9절).
def test_auto_buy_blocked_in_de_regime_even_with_high_recovery_score():
    trader = AutoTrader(broker=MagicMock())
    regime_result = {"regime": "D", "recovery_score": 80.0, "market_collapse_score": 60.0,
                      "semiconductor_collapse_score": 60.0, "data_quality_score": 90.0,
                      "predictions": {"30m": {"confidence_score": 80.0, "probability_up": 80.0}}}
    manual_only, reason = trader._auto_buy_recovery_gate(regime_result)
    assert manual_only is True
    assert "수동승인" in reason


def test_auto_buy_allowed_when_all_conditions_met():
    trader = AutoTrader(broker=MagicMock())
    regime_result = {"regime": "A", "recovery_score": 70.0, "market_collapse_score": 30.0,
                      "semiconductor_collapse_score": 30.0, "data_quality_score": 90.0,
                      "predictions": {"30m": {"confidence_score": 80.0, "probability_up": 80.0}}}
    manual_only, reason = trader._auto_buy_recovery_gate(regime_result)
    assert manual_only is False
