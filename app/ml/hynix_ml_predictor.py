"""hynix_ml_predictor.py — 학습된 horizon별 모델로 하이닉스 가격/방향을 예측한다.

historical_data_loader.collect_all_historical()로 얻은 최신 데이터를
feature_builder로 재가공해 "가장 최근 시점"의 feature 행으로 예측한다.
학습 시점의 feature_columns/결측 대체값(train_medians)을 model_registry
메타데이터에서 그대로 읽어 재사용해 학습/예측 시점의 feature 정합성을 보장한다.

학습된 모델이 없으면(아직 학습 전) 예외를 던지지 않고 available=False를
반환한다 — 호출부(ensemble_predictor)가 이를 "ML 없음, Rule 100%"로 처리한다.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

try:
    from app.logger import logger
except ImportError:
    logger = logging.getLogger(__name__)

import pandas as pd

from app.ml import feature_builder as fb
from app.ml import model_registry as registry

HORIZONS = ("30m", "1h", "3h", "close", "next_open")


def _estimate_confidence(coverage: float, clf_metrics: dict, below_min_samples: bool) -> float:
    base = 50.0 + coverage * 30.0
    acc = clf_metrics.get("accuracy")
    if acc is not None:
        base += (acc - 0.5) * 40.0
    if below_min_samples:
        base = min(base, 60.0)
    return round(max(0.0, min(95.0, base)), 1)


def _predict_one_horizon(horizon: str, latest_row: pd.Series) -> dict:
    reg_model, reg_meta = registry.load_model(horizon, "regressor")
    if reg_model is None or reg_meta is None:
        return {"available": False, "reason": "학습된 모델 없음(모델 학습 버튼 실행 필요)"}

    feature_columns = reg_meta.get("feature_columns", [])
    medians = reg_meta.get("train_medians", {})
    if not feature_columns:
        return {"available": False, "reason": "모델 메타데이터에 feature_columns 없음"}

    row = latest_row.reindex(feature_columns)
    missing = [c for c in feature_columns if c not in latest_row.index or pd.isna(latest_row.get(c))]
    for c in feature_columns:
        if pd.isna(row.get(c)):
            row[c] = medians.get(c, 0.0)
    X = pd.DataFrame([row[feature_columns].astype(float).values], columns=feature_columns)

    try:
        predicted_return = float(reg_model.predict(X)[0])
    except Exception as exc:
        return {"available": False, "reason": f"회귀 예측 실패: {exc}"}

    clf_model, clf_meta = registry.load_model(horizon, "direction")
    direction, probs = None, {"UP": None, "SIDEWAYS": None, "DOWN": None}
    if clf_model is not None:
        try:
            direction = str(clf_model.predict(X)[0])
            if hasattr(clf_model, "predict_proba"):
                proba = clf_model.predict_proba(X)[0]
                classes = list(clf_model.classes_)
                probs = {str(c): round(float(p) * 100, 1) for c, p in zip(classes, proba)}
        except Exception as exc:
            logger.debug("[HynixMLPredictor] 분류 예측 실패(%s): %s", horizon, exc)

    coverage = 1.0 - (len(missing) / max(len(feature_columns), 1))
    reg_metrics = reg_meta.get("metrics", {}) or {}
    clf_metrics = (clf_meta.get("metrics") if clf_meta else {}) or {}
    below_min = bool(reg_meta.get("below_min_samples", True))
    model_confidence = _estimate_confidence(coverage, clf_metrics, below_min)

    return {
        "available": True, "predicted_return_pct": round(predicted_return, 4),
        "direction": direction, "probability_up": probs.get("UP"),
        "probability_sideways": probs.get("SIDEWAYS"), "probability_down": probs.get("DOWN"),
        "model_confidence": model_confidence, "backend": reg_meta.get("backend"),
        "n_samples": reg_meta.get("n_samples"), "below_min_samples": below_min,
        "feature_coverage": round(coverage, 3), "missing_features": missing,
        "backtest_metrics": {"regressor": reg_metrics, "direction": clf_metrics},
        "feature_importance": reg_meta.get("feature_importance", {}),
        "trained_at": reg_meta.get("trained_at"),
    }


def predict_all_horizons_ml(historical_data: dict) -> dict:
    """historical_data: historical_data_loader.collect_all_historical() 결과.

    모델이 하나도 없으면(학습 전) 5개 horizon 전부 available=False로 채워
    반환한다 — 항상 dict 구조는 동일하게 유지해 호출부가 단순해진다.
    """
    daily = fb.build_daily_feature_table(historical_data)
    intraday = fb.build_intraday_feature_table(historical_data)

    result: dict = {"predicted_at": datetime.now().isoformat(timespec="seconds"), "horizons": {},
                     "has_any_trained_model": registry.has_trained_models()}

    if not daily["table"].empty:
        latest_daily = daily["table"].iloc[-1]
        for horizon in ("close", "next_open"):
            result["horizons"][horizon] = _predict_one_horizon(horizon, latest_daily)
    else:
        for horizon in ("close", "next_open"):
            result["horizons"][horizon] = {"available": False, "reason": "일봉 feature 없음(과거 데이터 수집 필요)"}

    if not intraday["table"].empty:
        latest_intraday = intraday["table"].iloc[-1]
        for horizon in ("30m", "1h", "3h"):
            result["horizons"][horizon] = _predict_one_horizon(horizon, latest_intraday)
    else:
        for horizon in ("30m", "1h", "3h"):
            result["horizons"][horizon] = {"available": False, "reason": "분봉 feature 없음 — Rule 예측에 의존"}

    return result
