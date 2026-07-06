"""ensemble_predictor.py — 기존 룰 기반 예측(hynix_price_predictor)과 신규
ML 예측(hynix_ml_predictor)을 동적 비중으로 앙상블한다.

룰 기반 모듈은 완전히 유지하고 삭제하지 않는다 — 이 모듈은 두 예측을
"더한" 결과만 추가로 제공하는 얇은 합성 계층이다. ML 예측이 없거나(학습
전) 신뢰도가 낮으면 자동으로 Rule 비중이 커진다(최악의 경우 Rule 100%).

앙상블 비중 원칙(config/ml_training.yaml: ensemble:):
  - 기본 50/50.
  - ML 백테스트 방향적중률 >= min_backtest_direction_accuracy 이고
    confidence >= min_ml_confidence 이면 ML 비중을 max_ml_weight까지 확대.
  - 데이터 부족/휴장/ML confidence 낮음/표본 부족(below_min_samples)이면
    Rule 비중을 max_rule_weight까지 확대.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Optional

import yaml

from app.models.hynix_predictor import _round_krx

ROOT = Path(__file__).resolve().parent.parent.parent
CONFIG_PATH = ROOT / "config" / "ml_training.yaml"

DEFAULT_ENSEMBLE_CFG = {
    "default_ml_weight": 0.5, "default_rule_weight": 0.5,
    "min_ml_confidence": 60.0, "min_backtest_direction_accuracy": 0.55,
    "max_ml_weight": 0.7, "max_rule_weight": 0.8,
}

_HORIZON_KEY_MAP = {
    "30m": {"ret": "expected_return_pct_30m", "price": "predicted_price_30m",
            "p_up": "probability_up_30m", "p_side": "probability_sideways_30m", "p_down": "probability_down_30m"},
    "1h": {"ret": "expected_return_pct_1h", "price": "predicted_price_1h",
           "p_up": "probability_up_1h", "p_side": "probability_sideways_1h", "p_down": "probability_down_1h"},
    "3h": {"ret": "expected_return_pct_3h", "price": "predicted_price_3h",
           "p_up": "probability_up_3h", "p_side": "probability_sideways_3h", "p_down": "probability_down_3h"},
    "close": {"ret": "expected_return_pct_close", "price": "predicted_close_today",
              "p_up": "probability_up_close", "p_side": "probability_sideways_close", "p_down": "probability_down_close"},
    "next_open": {"ret": "expected_return_pct_tomorrow_open", "price": "predicted_open_tomorrow",
                  "p_up": "probability_up_tomorrow_open", "p_side": "probability_sideways_tomorrow_open",
                  "p_down": "probability_down_tomorrow_open"},
}


def load_ensemble_config() -> dict:
    try:
        raw = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}
        return {**DEFAULT_ENSEMBLE_CFG, **(raw.get("ensemble") or {})}
    except Exception:
        return dict(DEFAULT_ENSEMBLE_CFG)


def extract_rule_horizon(rule_result: dict, horizon: str) -> dict:
    keys = _HORIZON_KEY_MAP[horizon]
    return {
        "expected_return_pct": rule_result.get(keys["ret"]),
        "predicted_price": rule_result.get(keys["price"]),
        "probability_up": rule_result.get(keys["p_up"]),
        "probability_sideways": rule_result.get(keys["p_side"]),
        "probability_down": rule_result.get(keys["p_down"]),
    }


def _compute_weights(ml_horizon: dict, cfg: dict, holiday_mode: bool) -> tuple:
    if not ml_horizon.get("available"):
        return 0.0, 1.0, "ML 예측 없음(학습 전/데이터 부족) — Rule 100% 사용"

    ml_confidence = ml_horizon.get("model_confidence", 0.0) or 0.0
    direction_acc = ((ml_horizon.get("backtest_metrics") or {}).get("direction") or {}).get("accuracy")
    below_min = bool(ml_horizon.get("below_min_samples", True))

    if (
        holiday_mode or below_min or ml_confidence < cfg["min_ml_confidence"]
        or (direction_acc is not None and direction_acc < cfg["min_backtest_direction_accuracy"])
    ):
        rule_w = cfg["max_rule_weight"]
        reason = "데이터 부족/휴장/ML 신뢰도 낮음 — Rule 비중 확대"
        return round(1 - rule_w, 3), round(rule_w, 3), reason

    if direction_acc is not None and direction_acc >= cfg["min_backtest_direction_accuracy"] and ml_confidence >= cfg["min_ml_confidence"]:
        ml_w = min(cfg["max_ml_weight"], cfg["default_ml_weight"] + max(0.0, direction_acc - 0.5))
        return round(ml_w, 3), round(1 - ml_w, 3), "최근 ML 백테스트 성과 양호 — ML 비중 확대"

    return cfg["default_ml_weight"], cfg["default_rule_weight"], "기본 50/50 비중"


def _blend_prob(rule_v: Optional[float], ml_v: Optional[float], rule_w: float, ml_w: float) -> Optional[float]:
    if rule_v is None and ml_v is None:
        return None
    if ml_v is None:
        return rule_v
    if rule_v is None:
        return ml_v
    return round(rule_v * rule_w + ml_v * ml_w, 1)


def ensemble_horizon(horizon: str, rule_result: dict, ml_result: dict, base_price: Optional[float],
                      holiday_mode: bool, cfg: Optional[dict] = None) -> dict:
    cfg = cfg or load_ensemble_config()
    rule_slice = extract_rule_horizon(rule_result, horizon)
    ml_horizon = (ml_result or {}).get("horizons", {}).get(horizon, {"available": False, "reason": "ML 결과 없음"})

    ml_weight, rule_weight, reason = _compute_weights(ml_horizon, cfg, holiday_mode)

    rule_return = rule_slice.get("expected_return_pct")
    ml_return = ml_horizon.get("predicted_return_pct") if ml_horizon.get("available") else None

    if ml_return is None:
        ensemble_return = rule_return
    elif rule_return is None:
        ensemble_return = ml_return
    else:
        ensemble_return = rule_return * rule_weight + ml_return * ml_weight

    ensemble_price = None
    if ensemble_return is not None and base_price:
        ensemble_price = _round_krx(base_price * (1 + ensemble_return / 100))

    p_up = _blend_prob(rule_slice.get("probability_up"), ml_horizon.get("probability_up"), rule_weight, ml_weight)
    p_down = _blend_prob(rule_slice.get("probability_down"), ml_horizon.get("probability_down"), rule_weight, ml_weight)
    p_side = _blend_prob(rule_slice.get("probability_sideways"), ml_horizon.get("probability_sideways"), rule_weight, ml_weight)
    if p_up is not None and p_down is not None and p_side is not None:
        total = p_up + p_down + p_side
        if total > 0:
            p_up, p_down, p_side = (round(p_up / total * 100, 1), round(p_down / total * 100, 1), round(p_side / total * 100, 1))

    return {
        "horizon": horizon,
        "rule_return_pct": rule_return, "rule_price": rule_slice.get("predicted_price"),
        "ml_return_pct": ml_horizon.get("predicted_return_pct"), "ml_price": (
            _round_krx(base_price * (1 + ml_horizon["predicted_return_pct"] / 100))
            if ml_horizon.get("available") and base_price else None
        ),
        "ensemble_return_pct": round(ensemble_return, 4) if ensemble_return is not None else None,
        "ensemble_price": ensemble_price,
        "final_price": ensemble_price if ensemble_price is not None else rule_slice.get("predicted_price"),
        "ml_weight": ml_weight, "rule_weight": rule_weight, "weight_reason": reason,
        "ml_available": ml_horizon.get("available", False),
        "ml_confidence": ml_horizon.get("model_confidence"),
        "ml_below_min_samples": ml_horizon.get("below_min_samples"),
        "ml_backtest_metrics": ml_horizon.get("backtest_metrics"),
        "probability_up": p_up, "probability_sideways": p_side, "probability_down": p_down,
    }


DEFAULT_AUTO_GATE_CFG = {
    "min_recent_3m_direction_accuracy": 0.60, "max_30m_mape_pct": 0.8,
    "min_data_quality_score": 75, "min_ensemble_confidence": 65,
    "min_recovery_score": 60, "max_collapse_score": 70,
}


def load_auto_gate_config() -> dict:
    try:
        raw = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}
        return {**DEFAULT_AUTO_GATE_CFG, **(raw.get("auto_trade_ml_gate") or {})}
    except Exception:
        return dict(DEFAULT_AUTO_GATE_CFG)


def check_ml_auto_trade_gate(
    ensemble_horizon_30m: dict, data_quality_score: Optional[float], current_regime: Optional[str],
    recovery_score: Optional[float], collapse_score: Optional[float],
    recent_3m_direction_accuracy: Optional[float] = None, mape_30m_pct: Optional[float] = None,
    cfg: Optional[dict] = None,
) -> tuple:
    """
    ML 예측을 자동매수 판단에 "참고"라도 쓰려면 만족해야 하는 조건(명세 9절).
    하나라도 미달이면 (blocked=True, reason)을 반환한다 — 이 경우 호출부는
    WATCH_ONLY 또는 MANUAL_APPROVAL로만 후보를 표시해야 하며, AUTO 매수에는
    절대 연결하지 않는다. 기존 자동손절/방어청산 로직에는 영향을 주지 않는다
    (이 함수는 신규 매수 판단에만 관여한다).
    """
    cfg = cfg or load_auto_gate_config()

    if recent_3m_direction_accuracy is None:
        recent_3m_direction_accuracy = (
            (ensemble_horizon_30m.get("ml_backtest_metrics") or {}).get("direction") or {}
        ).get("accuracy")

    checks = [
        (recent_3m_direction_accuracy is not None and recent_3m_direction_accuracy >= cfg["min_recent_3m_direction_accuracy"],
         f"최근 3개월 방향적중률 {recent_3m_direction_accuracy}"),
        (mape_30m_pct is not None and mape_30m_pct <= cfg["max_30m_mape_pct"], f"30분 MAPE {mape_30m_pct}%"),
        (data_quality_score is not None and data_quality_score >= cfg["min_data_quality_score"],
         f"data_quality_score {data_quality_score}"),
        (ensemble_horizon_30m.get("ml_confidence") is not None and ensemble_horizon_30m["ml_confidence"] >= cfg["min_ensemble_confidence"],
         f"ensemble_confidence {ensemble_horizon_30m.get('ml_confidence')}"),
        (current_regime not in ("D", "E"), f"current_regime {current_regime}"),
        (recovery_score is not None and recovery_score >= cfg["min_recovery_score"], f"recovery_score {recovery_score}"),
        (collapse_score is not None and collapse_score < cfg["max_collapse_score"], f"collapse_score {collapse_score}"),
    ]
    failed = [label for ok, label in checks if not ok]
    if failed:
        return True, "ML 참고 자동매수 조건 미달(WATCH_ONLY/MANUAL_APPROVAL만 허용): " + ", ".join(failed)
    return False, "ML 참고 자동매수 조건 충족"


def build_ensemble_result(rule_result: dict, ml_result: dict, holiday_mode: bool = False,
                           cfg: Optional[dict] = None) -> dict:
    """rule_result: hynix_price_predictor.predict()의 전체 결과.
    ml_result: hynix_ml_predictor.predict_all_horizons_ml()의 전체 결과."""
    cfg = cfg or load_ensemble_config()
    base_price = rule_result.get("base_price")

    horizons = {}
    for horizon in ("30m", "1h", "3h", "close", "next_open"):
        horizons[horizon] = ensemble_horizon(horizon, rule_result, ml_result, base_price, holiday_mode, cfg)

    return {
        "base_price": base_price, "holiday_mode": holiday_mode, "horizons": horizons,
        "has_any_trained_model": (ml_result or {}).get("has_any_trained_model", False),
    }
