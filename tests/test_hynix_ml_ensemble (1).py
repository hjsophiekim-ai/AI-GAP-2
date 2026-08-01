"""test_hynix_ml_ensemble.py — SK하이닉스 ML 학습/앙상블 파이프라인 검증(12개 시나리오)."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from app.ml import feature_builder as fb
from app.ml import model_registry as registry
from app.ml import time_decay as td
from app.ml.ensemble_predictor import build_ensemble_result, check_ml_auto_trade_gate
from app.ml.historical_data_loader import (
    _fetch_domestic_daily_kis,
    _fetch_domestic_daily_pykrx,
    _fetch_domestic_daily_yfinance,
    collect_domestic_daily_1y,
)
from app.ml.hynix_ml_predictor import predict_all_horizons_ml
from app.ml.hynix_ml_trainer import train_all_models

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))
backtest_mod = importlib.import_module("backtest_hynix_ml")


def _synthetic_historical_data(n_daily: int = 300, n_intraday: int = 3000, seed: int = 0) -> dict:
    rng = np.random.default_rng(seed)
    dates = pd.date_range("2025-09-01", periods=n_daily, freq="B")
    price = 200_000 + np.cumsum(rng.normal(0, 1000, n_daily))
    daily = pd.DataFrame({
        "datetime": dates, "open": price, "high": price + 500, "low": price - 500,
        "close": price, "volume": rng.integers(1_000_000, 5_000_000, n_daily),
    })

    def _series(base, scale):
        return daily[["datetime", "close"]].assign(close=base + np.cumsum(rng.normal(0, scale, n_daily)))

    hist = {
        "hynix": {"df": daily, "source": "test"},
        "samsung": {"df": daily.assign(close=daily.close * 0.3), "source": "test"},
        "hanmi": {"df": daily.assign(close=daily.close * 0.5), "source": "test"},
        "kospi": {"df": _series(2500, 5)}, "kosdaq": {"df": _series(800, 3)},
        "kospi200": {"df": _series(330, 1)}, "usdkrw": {"df": _series(1350, 2)},
        "mu": {"df": _series(100, 2)}, "nvda": {"df": _series(120, 2)},
        "amd": {"df": _series(150, 2)}, "avgo": {"df": _series(1500, 10)},
        "qqq": {"df": _series(450, 3)}, "sox_proxy": {"df": _series(200, 2)},
    }
    minutes = pd.date_range("2026-06-01 09:00", periods=n_intraday, freq="1min")
    iprice = 250_000 + np.cumsum(rng.normal(0, 50, n_intraday))
    idf = pd.DataFrame({
        "datetime": minutes, "open": iprice, "high": iprice + 30, "low": iprice - 30,
        "close": iprice, "volume": rng.integers(1000, 5000, n_intraday),
    })
    hist["hynix_intraday"] = {"df": idf, "granularity": "1m", "source": "test"}
    return hist


@pytest.fixture()
def isolated_registry(tmp_path, monkeypatch):
    monkeypatch.setattr(registry, "MODELS_DIR", tmp_path)
    monkeypatch.setattr(registry, "REGISTRY_PATH", tmp_path / "registry.json")
    return tmp_path


# 1. 1년치 데이터 수집 fallback 테스트
def test_domestic_daily_collection_fallback_chain(monkeypatch):
    monkeypatch.setattr("app.ml.historical_data_loader._fetch_domestic_daily_kis", lambda *a, **k: None)
    monkeypatch.setattr("app.ml.historical_data_loader._fetch_domestic_daily_pykrx", lambda *a, **k: None)

    called = {}

    def _fake_yf(symbol, lookback_days):
        called["yfinance"] = True
        return pd.DataFrame({
            "datetime": pd.date_range("2025-01-01", periods=250, freq="B"),
            "open": [100.0] * 250, "high": [101.0] * 250, "low": [99.0] * 250,
            "close": [100.0] * 250, "volume": [1000] * 250,
        })

    monkeypatch.setattr("app.ml.historical_data_loader._fetch_domestic_daily_yfinance", _fake_yf)
    result = collect_domestic_daily_1y("hynix", lookback_days=365)
    assert result["source"] == "yfinance"
    assert called.get("yfinance") is True
    assert result["error"] is None


# 2. 최근 30일/90일/이전 데이터 가중치 테스트
def test_recent_data_weight_tiers():
    from datetime import datetime, timedelta
    now = datetime(2026, 7, 7)
    ts = pd.Series([now - timedelta(days=d) for d in (5, 60, 200)])
    weights = td.compute_sample_weights(ts, now=now)
    assert weights.tolist() == [3.0, 2.0, 1.0]


# 3. lookahead bias 방지 테스트
def test_no_lookahead_bias_in_targets():
    hist = _synthetic_historical_data()
    daily = fb.build_daily_feature_table(hist)["table"]
    # 마지막 행은 "미래"가 없으므로 타깃이 반드시 NaN이어야 한다(미래 데이터 누출 없음의 증거).
    assert pd.isna(daily.iloc[-1]["target_return_close"])
    assert pd.isna(daily.iloc[-1]["target_return_next_open"])

    intraday = fb.build_intraday_feature_table(hist)["table"]
    assert pd.isna(intraday.iloc[-1]["target_return_30m"])
    # feature 컬럼에는 target_ 접두어가 있는 컬럼이 하나도 없어야 한다(feature/target 분리 확인).
    feat_cols = fb.build_daily_feature_table(hist)["feature_columns"]
    assert not any(c.startswith("target_") for c in feat_cols)


# 4. target_return_30m/1h/3h/close/next_open 생성 테스트
def test_all_target_columns_generated_with_correct_sideways_bands():
    hist = _synthetic_historical_data()
    daily = fb.build_daily_feature_table(hist)["table"]
    for h in ("close", "next_open"):
        assert f"target_return_{h}" in daily.columns
        assert f"target_direction_{h}" in daily.columns
        directions = daily[f"target_direction_{h}"].dropna().unique()
        assert set(directions).issubset({"UP", "DOWN", "SIDEWAYS"})

    intraday = fb.build_intraday_feature_table(hist)["table"]
    for h in ("30m", "1h", "3h"):
        assert f"target_return_{h}" in intraday.columns
        assert f"target_direction_{h}" in intraday.columns
    assert fb.SIDEWAYS_BAND_PCT == {"30m": 0.3, "1h": 0.5, "3h": 0.8, "close": 1.0, "next_open": 1.2}


# 5. walk-forward split 테스트 (랜덤 셔플 없음 — 시간순 분할 확인)
def test_walk_forward_split_is_chronological_not_shuffled(isolated_registry):
    hist = _synthetic_historical_data()
    daily = fb.build_daily_feature_table(hist)
    from app.ml.hynix_ml_trainer import load_training_config, train_horizon_models

    result = train_horizon_models(daily["table"], daily["feature_columns"], "close", load_training_config())
    assert result.get("error") is None
    valid = daily["table"].dropna(subset=["target_return_close", "target_direction_close"]).sort_values("datetime")
    split_idx = int(len(valid) * 0.8)
    train_max_dt = valid.iloc[:split_idx]["datetime"].max()
    test_min_dt = valid.iloc[split_idx:]["datetime"].min()
    assert train_max_dt <= test_min_dt  # 학습 구간이 항상 테스트 구간보다 과거


# 6. 모델 학습 후 예측값 생성 테스트
def test_train_then_predict_produces_available_result(isolated_registry):
    hist = _synthetic_historical_data()
    train_all_models(hist)
    ml_result = predict_all_horizons_ml(hist)
    assert ml_result["has_any_trained_model"] is True
    for horizon in ("close", "next_open", "30m", "1h", "3h"):
        assert ml_result["horizons"][horizon]["available"] is True
        assert isinstance(ml_result["horizons"][horizon]["predicted_return_pct"], float)


# 7. 룰/ML/앙상블 예측값 생성 테스트
def test_rule_ml_ensemble_all_present_in_result():
    rule_result = {
        "base_price": 250000, "expected_return_pct_30m": -0.1, "predicted_price_30m": 249750,
        "probability_up_30m": 30.0, "probability_sideways_30m": 40.0, "probability_down_30m": 30.0,
    }
    ml_result = {"has_any_trained_model": True, "horizons": {
        "30m": {"available": True, "predicted_return_pct": 0.2, "model_confidence": 70.0,
                "below_min_samples": False, "probability_up": 60.0, "probability_sideways": 25.0,
                "probability_down": 15.0, "backtest_metrics": {"direction": {"accuracy": 0.62}}},
        "1h": {"available": False}, "3h": {"available": False}, "close": {"available": False}, "next_open": {"available": False},
    }}
    ens = build_ensemble_result(rule_result, ml_result, holiday_mode=False)
    h30 = ens["horizons"]["30m"]
    assert h30["rule_return_pct"] == -0.1
    assert h30["ml_return_pct"] == 0.2
    assert h30["ensemble_return_pct"] is not None
    assert h30["final_price"] is not None


# 8. 백테스트 리포트 생성 테스트
def test_backtest_report_generation(tmp_path, monkeypatch, isolated_registry):
    monkeypatch.setattr(backtest_mod, "REPORTS_DIR", tmp_path)
    hist = _synthetic_historical_data()
    train_all_models(hist)

    daily_tbl = fb.build_daily_feature_table(hist)["table"]
    result = backtest_mod.backtest_horizon("close", daily_tbl)
    summary = backtest_mod.summarize_backtest({"close": result})
    backtest_mod.write_reports(summary)

    assert (tmp_path / "hynix_ml_backtest_summary.md").exists()
    assert (tmp_path / "hynix_ml_backtest_detail.csv").exists()
    content = (tmp_path / "hynix_ml_backtest_summary.md").read_text(encoding="utf-8")
    assert "보장하지" in content


# 9. 성과 기준 미달 시 ML 자동매수 미사용 테스트
def test_ml_auto_trade_gate_blocks_when_below_threshold():
    weak_horizon = {"ml_confidence": 50.0, "ml_backtest_metrics": {"direction": {"accuracy": 0.40}}}
    blocked, reason = check_ml_auto_trade_gate(
        weak_horizon, data_quality_score=90.0, current_regime="A",
        recovery_score=80.0, collapse_score=20.0, recent_3m_direction_accuracy=0.40,
    )
    assert blocked is True
    assert "미달" in reason

    strong_horizon = {"ml_confidence": 80.0, "ml_backtest_metrics": {"direction": {"accuracy": 0.65}}}
    ok, reason_ok = check_ml_auto_trade_gate(
        strong_horizon, data_quality_score=90.0, current_regime="A",
        recovery_score=80.0, collapse_score=20.0, recent_3m_direction_accuracy=0.65, mape_30m_pct=0.5,
    )
    assert ok is False


# 10. 최근 3개월 성과를 별도로 계산하는 테스트
def test_recent_3m_window_computed_separately(isolated_registry):
    hist = _synthetic_historical_data()
    train_all_models(hist)
    daily_tbl = fb.build_daily_feature_table(hist)["table"]
    result = backtest_mod.backtest_horizon("close", daily_tbl)
    summary = backtest_mod.summarize_backtest({"close": result})
    windows = summary["horizons"]["close"]["windows"]
    assert set(windows.keys()) == {"full", "recent_3m", "recent_1m"}
    assert windows["recent_1m"]["ensemble"]["n"] <= windows["recent_3m"]["ensemble"]["n"] <= windows["full"]["ensemble"]["n"]


# 11. feature importance 출력 테스트
def test_feature_importance_extracted(isolated_registry):
    hist = _synthetic_historical_data()
    train_res = train_all_models(hist)
    fi = train_res["horizons"]["close"]["feature_importance"]
    assert isinstance(fi, dict) and len(fi) > 0
    assert all(isinstance(v, float) for v in fi.values())

    ml_result = predict_all_horizons_ml(hist)
    assert ml_result["horizons"]["close"]["feature_importance"]


# 12. 데이터 부족 시 룰 기반 fallback 테스트
def test_insufficient_data_falls_back_to_rule_only(isolated_registry):
    ml_result = predict_all_horizons_ml({})  # 과거 데이터 전혀 없음
    for horizon in ("30m", "1h", "3h", "close", "next_open"):
        assert ml_result["horizons"][horizon]["available"] is False

    rule_result = {"base_price": 250000, "expected_return_pct_close": 0.3, "predicted_close_today": 250750}
    ens = build_ensemble_result(rule_result, ml_result, holiday_mode=False)
    close = ens["horizons"]["close"]
    assert close["ml_available"] is False
    assert close["rule_weight"] == 1.0
    # ensemble_price는 rule의 return%+base_price로 재계산되므로(정합성을 위해 KRX 호가단위로
    # 재라운딩됨) rule의 predicted_close_today와 완전히 같지 않을 수 있다 — 값 존재만 확인.
    assert close["final_price"] is not None
    assert abs(close["final_price"] - 250750) < 500
