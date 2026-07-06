"""hynix_ml_trainer.py — horizon별(30m/1h/3h/close/next_open) 회귀+분류 모델 학습.

모델 백엔드 우선순위: LightGBM -> XGBoost -> scikit-learn
HistGradientBoosting -> Ridge/LogisticRegression(baseline). 환경에 설치된
라이브러리에 따라 자동 선택되며, 어떤 조합이든 동일한 인터페이스로 동작한다.

walk-forward(시간순 앞 80% 학습 / 뒤 20% 평가, 랜덤 셔플 없음)로 검증하고,
표본이 너무 적으면(<30) 학습 자체를 생략해 결과에 error를 남긴다.
config/ml_training.yaml의 min_samples 미달 시에도 학습은 진행하되
below_min_samples=True로 표시한다(완전히 막지는 않음 — 앙상블 단계에서
신뢰도로 반영).
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

try:
    from app.logger import logger
except ImportError:
    logger = logging.getLogger(__name__)

import numpy as np
import pandas as pd
import yaml

from app.ml import feature_builder as fb
from app.ml import model_registry as registry
from app.ml import time_decay as td

ROOT = Path(__file__).resolve().parent.parent.parent
CONFIG_PATH = ROOT / "config" / "ml_training.yaml"

DEFAULT_TRAINING_CFG = {
    "lookback_days": 365, "recent_30d_weight": 3.0, "recent_90d_weight": 2.0,
    "older_weight": 1.0, "use_exponential_decay": True, "decay_half_life_days": 60,
    "train_test_split": "walk_forward", "min_samples": 500,
}

DAILY_HORIZONS = ("close", "next_open")
INTRADAY_HORIZONS = ("30m", "1h", "3h")


def load_training_config() -> dict:
    try:
        raw = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}
        return {**DEFAULT_TRAINING_CFG, **(raw.get("training") or {})}
    except Exception as exc:
        logger.debug("[HynixMLTrainer] 학습 설정 로드 실패, 기본값 사용: %s", exc)
        return dict(DEFAULT_TRAINING_CFG)


def _get_model_backend():
    try:
        from lightgbm import LGBMClassifier, LGBMRegressor
        return LGBMRegressor, LGBMClassifier, "lightgbm"
    except Exception:
        pass
    try:
        import xgboost as xgb
        return xgb.XGBRegressor, xgb.XGBClassifier, "xgboost"
    except Exception:
        pass
    try:
        from sklearn.ensemble import HistGradientBoostingClassifier, HistGradientBoostingRegressor
        return HistGradientBoostingRegressor, HistGradientBoostingClassifier, "hist_gradient_boosting"
    except Exception:
        from sklearn.linear_model import LogisticRegression, Ridge
        return Ridge, LogisticRegression, "linear_baseline"


def _fit_regressor(cls, backend: str, X: pd.DataFrame, y: pd.Series, weights: pd.Series):
    kwargs = {}
    if backend == "lightgbm":
        model = cls(n_estimators=200, max_depth=6, learning_rate=0.05, verbosity=-1)
    elif backend == "xgboost":
        model = cls(n_estimators=200, max_depth=6, learning_rate=0.05, verbosity=0)
    elif backend == "hist_gradient_boosting":
        model = cls(max_iter=200, max_depth=6, learning_rate=0.05)
    else:
        model = cls(alpha=1.0)
    try:
        model.fit(X, y, sample_weight=weights)
    except TypeError:
        model.fit(X, y)
    return model


def _fit_classifier(cls, backend: str, X: pd.DataFrame, y: pd.Series, weights: pd.Series):
    if backend == "lightgbm":
        model = cls(n_estimators=200, max_depth=6, learning_rate=0.05, verbosity=-1)
    elif backend == "xgboost":
        model = cls(n_estimators=200, max_depth=6, learning_rate=0.05, verbosity=0)
    elif backend == "hist_gradient_boosting":
        model = cls(max_iter=200, max_depth=6, learning_rate=0.05)
    else:
        model = cls(max_iter=1000)
    try:
        model.fit(X, y, sample_weight=weights)
    except TypeError:
        model.fit(X, y)
    return model


def _extract_feature_importance(model, feature_columns: list, backend: str) -> dict:
    try:
        if hasattr(model, "feature_importances_"):
            values = model.feature_importances_
        elif hasattr(model, "coef_"):
            coef = model.coef_
            values = np.abs(coef[0]) if getattr(coef, "ndim", 1) > 1 else np.abs(coef)
        else:
            return {}
        pairs = sorted(zip(feature_columns, [float(v) for v in values]), key=lambda kv: kv[1], reverse=True)
        return dict(pairs)
    except Exception as exc:
        logger.debug("[HynixMLTrainer] feature importance 추출 실패: %s", exc)
        return {}


def train_horizon_models(table: pd.DataFrame, feature_columns: list, horizon: str, training_cfg: dict) -> dict:
    result: dict = {"horizon": horizon}
    reg_col, dir_col = f"target_return_{horizon}", f"target_direction_{horizon}"
    if reg_col not in table.columns or dir_col not in table.columns:
        result["error"] = f"target 컬럼 없음({reg_col}/{dir_col})"
        return result

    valid = table.dropna(subset=[reg_col, dir_col]).sort_values("datetime").reset_index(drop=True)
    n = len(valid)
    result["n_samples"] = n
    result["below_min_samples"] = n < training_cfg.get("min_samples", 500)

    if n < 30:
        result["error"] = f"표본 부족({n}건) — 학습 생략"
        return result

    feat_cols = [c for c in feature_columns if c in valid.columns and valid[c].notna().any()]
    if not feat_cols:
        result["error"] = "사용 가능한 feature 없음"
        return result

    split_idx = int(n * 0.8)
    train_df, test_df = valid.iloc[:split_idx].copy(), valid.iloc[split_idx:].copy()
    if len(train_df) < 20 or len(test_df) < 5:
        train_df, test_df = valid, valid.iloc[0:0]

    medians = train_df[feat_cols].median()
    X_train = train_df[feat_cols].fillna(medians).fillna(0.0)
    X_test = test_df[feat_cols].fillna(medians).fillna(0.0) if len(test_df) else None

    weights = td.compute_sample_weights(
        train_df["datetime"], now=valid["datetime"].max(),
        use_exponential_decay=training_cfg.get("use_exponential_decay", True),
        recent_30d_weight=training_cfg.get("recent_30d_weight", 3.0),
        recent_90d_weight=training_cfg.get("recent_90d_weight", 2.0),
        older_weight=training_cfg.get("older_weight", 1.0),
        decay_half_life_days=training_cfg.get("decay_half_life_days", 60),
    ).values

    RegCls, ClfCls, backend = _get_model_backend()

    try:
        reg_model = _fit_regressor(RegCls, backend, X_train, train_df[reg_col], weights)
    except Exception as exc:
        result["error"] = f"회귀모델 학습 실패: {exc}"
        return result

    reg_metrics: dict = {}
    if X_test is not None and len(X_test):
        pred = reg_model.predict(X_test)
        y_test = test_df[reg_col].values
        reg_metrics = {
            "mae": round(float(np.mean(np.abs(pred - y_test))), 4),
            "rmse": round(float(np.sqrt(np.mean((pred - y_test) ** 2))), 4),
            "n_test": len(y_test),
        }

    try:
        clf_model = _fit_classifier(ClfCls, backend, X_train, train_df[dir_col], weights)
    except Exception as exc:
        result["error"] = f"분류모델 학습 실패: {exc}"
        clf_model = None

    clf_metrics: dict = {}
    if clf_model is not None and X_test is not None and len(X_test):
        pred_dir = clf_model.predict(X_test)
        y_test_dir = test_df[dir_col].values
        clf_metrics = {
            "accuracy": round(float(np.mean(pred_dir == y_test_dir)), 4),
            "n_test": len(y_test_dir),
        }

    feature_importance = _extract_feature_importance(reg_model, feat_cols, backend)

    metadata_common = {
        "backend": backend, "n_samples": n, "below_min_samples": result["below_min_samples"],
        "feature_columns": feat_cols, "train_medians": medians.to_dict(),
        "trained_at": datetime.now().isoformat(timespec="seconds"),
        "feature_importance": feature_importance,
    }
    registry.save_model(horizon, "regressor", reg_model, {**metadata_common, "metrics": reg_metrics})
    if clf_model is not None:
        registry.save_model(horizon, "direction", clf_model, {**metadata_common, "metrics": clf_metrics})

    result.update({
        "backend": backend, "regressor_metrics": reg_metrics, "direction_metrics": clf_metrics,
        "feature_importance": feature_importance,
    })
    return result


def train_all_models(historical_data: dict, training_cfg: Optional[dict] = None) -> dict:
    """모든 horizon(30m/1h/3h/close/next_open)의 회귀+분류 모델을 학습한다."""
    training_cfg = training_cfg or load_training_config()

    daily = fb.build_daily_feature_table(historical_data)
    intraday = fb.build_intraday_feature_table(historical_data)

    results: dict = {"trained_at": datetime.now().isoformat(timespec="seconds"), "horizons": {}, "warnings": []}
    results["warnings"].extend(daily.get("warnings", []))
    results["warnings"].extend(intraday.get("warnings", []))

    for horizon in DAILY_HORIZONS:
        if daily["table"].empty:
            results["horizons"][horizon] = {"horizon": horizon, "error": "일봉 feature 테이블이 비어 있음"}
            continue
        results["horizons"][horizon] = train_horizon_models(daily["table"], daily["feature_columns"], horizon, training_cfg)

    for horizon in INTRADAY_HORIZONS:
        if intraday["table"].empty:
            results["horizons"][horizon] = {"horizon": horizon, "error": "분봉 feature 테이블이 비어 있음(Rule 예측에 의존 필요)"}
            continue
        results["horizons"][horizon] = train_horizon_models(intraday["table"], intraday["feature_columns"], horizon, training_cfg)

    return results
