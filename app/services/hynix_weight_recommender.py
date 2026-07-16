"""
hynix_weight_recommender.py — 최근 5거래일 로그 상관분석 기반 가중치 추천.

enhanced_score 가중합에 실제 참여하는 4개 가중치(base_prediction/existing_micron/
hynix_technical/intraday_momentum)만 조정 대상이며, inverse_pressure_score는
참고용 상관계수만 함께 보고한다(가중합에 포함되지 않는 별도 판단 입력이므로).
자동 반영하지 않고 `data/state/hynix_weight_recommendation.json`에 추천값만 저장한다.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import pandas as pd

from app.logger import logger
from app.services.hynix_prediction_tracker import (
    _read_decision_log_for_date, _read_outcome_log_for_dates, SCORE_COLUMNS,
    compute_score_outcome_correlations,
)

from app.utils.data_paths import STATE_DIR

ROOT = Path(__file__).resolve().parent.parent.parent
_RECOMMENDATION_PATH = STATE_DIR / "hynix_weight_recommendation.json"

_WEIGHTED_COLUMNS = ["base_prediction_score", "existing_micron_score", "hynix_technical_score", "intraday_momentum_score"]
_WEIGHT_KEY_BY_COLUMN = {
    "base_prediction_score": "base_prediction",
    "existing_micron_score": "existing_micron",
    "hynix_technical_score": "hynix_technical",
    "intraday_momentum_score": "intraday_momentum",
}

MIN_SAMPLE_SIZE = 100
LOOKBACK_TRADING_DAYS = 5
MAX_DELTA = 0.05
MIN_WEIGHT = 0.05
MAX_WEIGHT = 0.70
_CORR_TO_DELTA_SCALE = 0.15


def _recent_trading_dates(n: int = LOOKBACK_TRADING_DAYS) -> list[str]:
    """decision log에 실제 존재하는 날짜 중 최근 n개(거래일)."""
    from app.services.hynix_prediction_tracker import _DECISION_LOG_PATH

    if not _DECISION_LOG_PATH.exists():
        return []
    try:
        df = pd.read_csv(_DECISION_LOG_PATH, usecols=["timestamp"])
        df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce")
        df = df.dropna(subset=["timestamp"])
        dates = sorted(df["timestamp"].dt.strftime("%Y%m%d").unique().tolist())
        return dates[-n:]
    except Exception as exc:
        logger.debug("[WeightRecommender] 최근 거래일 조회 실패: %s", exc)
        return []


def _current_weighted_weights() -> dict:
    try:
        from app.services.hynix_weight_manager import get_active_weights

        weights = get_active_weights()
        return {k: float(weights.get(k, 0.25)) for k in _WEIGHT_KEY_BY_COLUMN.values()}
    except Exception as exc:
        logger.debug("[WeightRecommender] 현재 가중치 조회 실패: %s", exc)
        return {"base_prediction": 0.45, "existing_micron": 0.20, "hynix_technical": 0.25, "intraday_momentum": 0.10}


def recommend_weight_adjustment() -> dict:
    """최근 5거래일 로그를 분석해 가중치 추천값을 계산하고 파일로 저장한다(자동 반영 없음)."""
    dates = _recent_trading_dates()
    decisions = pd.concat([_read_decision_log_for_date(d) for d in dates], ignore_index=True) if dates else pd.DataFrame()
    outcomes = _read_outcome_log_for_dates(dates) if dates else pd.DataFrame()

    correlations = compute_score_outcome_correlations(decisions, outcomes, horizon_minutes=30)

    sample_size = 0
    try:
        if not decisions.empty and not outcomes.empty:
            dec = decisions.copy()
            dec["timestamp"] = pd.to_datetime(dec["timestamp"], errors="coerce")
            out = outcomes[outcomes["horizon_minutes"].astype(str) == "30"].copy()
            out["decision_timestamp"] = pd.to_datetime(out["decision_timestamp"], errors="coerce")
            joined = dec.merge(out, left_on="timestamp", right_on="decision_timestamp", how="inner")
            sample_size = int(joined.dropna(subset=["hynix_return_pct"]).shape[0])
    except Exception as exc:
        logger.debug("[WeightRecommender] 샘플 수 계산 실패: %s", exc)

    created_at = datetime.now().isoformat()
    current_weights = _current_weighted_weights()

    if sample_size < MIN_SAMPLE_SIZE:
        result = {
            "skipped": True,
            "reason": f"샘플 부족(sample_size={sample_size} < {MIN_SAMPLE_SIZE}) — 가중치 추천 생략",
            "current_weights": current_weights,
            "recommended_weights": None,
            "sample_size": sample_size,
            "expected_improvement": None,
            "correlations": correlations,
            "created_at": created_at,
        }
        _save(result)
        return result

    deltas = {}
    for col in _WEIGHTED_COLUMNS:
        corr = correlations.get(col) or 0.0
        deltas[col] = max(-MAX_DELTA, min(MAX_DELTA, corr * _CORR_TO_DELTA_SCALE))

    raw_new = {}
    for col in _WEIGHTED_COLUMNS:
        key = _WEIGHT_KEY_BY_COLUMN[col]
        raw_new[key] = max(MIN_WEIGHT, min(MAX_WEIGHT, current_weights[key] + deltas[col]))

    total = sum(raw_new.values()) or 1.0
    normalized = {k: v / total for k, v in raw_new.items()}

    # ±5%p 제한을 정규화 이후에도 최대한 보존 (2차 클램프 + 재정규화)
    clamped = {k: max(current_weights[k] - MAX_DELTA, min(current_weights[k] + MAX_DELTA, v)) for k, v in normalized.items()}
    total2 = sum(clamped.values()) or 1.0
    recommended_weights = {k: round(v / total2, 4) for k, v in clamped.items()}

    expected_improvement = round(
        sum(recommended_weights[_WEIGHT_KEY_BY_COLUMN[c]] * (correlations.get(c) or 0.0) for c in _WEIGHTED_COLUMNS)
        - sum(current_weights[_WEIGHT_KEY_BY_COLUMN[c]] * (correlations.get(c) or 0.0) for c in _WEIGHTED_COLUMNS),
        4,
    )

    movers = sorted(
        ((col, correlations.get(col) or 0.0) for col in _WEIGHTED_COLUMNS),
        key=lambda x: x[1], reverse=True,
    )
    best_col, best_corr = movers[0]
    worst_col, worst_corr = movers[-1]
    reason = (
        f"최근 {len(dates)}거래일(샘플 {sample_size}건) 기준: {best_col} 상관 {best_corr:+.3f}로 가장 높아 비중 확대, "
        f"{worst_col} 상관 {worst_corr:+.3f}로 가장 낮아 비중 축소 권장. 변경폭은 ±{MAX_DELTA*100:.0f}%p 이내로 제한."
    )

    result = {
        "skipped": False,
        "reason": reason,
        "current_weights": current_weights,
        "recommended_weights": recommended_weights,
        "sample_size": sample_size,
        "expected_improvement": expected_improvement,
        "correlations": correlations,
        "created_at": created_at,
    }
    _save(result)
    return result


def _save(result: dict) -> None:
    try:
        _RECOMMENDATION_PATH.parent.mkdir(parents=True, exist_ok=True)
        _RECOMMENDATION_PATH.write_text(json.dumps(result, ensure_ascii=False, default=str, indent=2), encoding="utf-8")
    except Exception as exc:
        logger.debug("[WeightRecommender] 추천 결과 저장 실패: %s", exc)


def load_recommendation() -> Optional[dict]:
    try:
        if not _RECOMMENDATION_PATH.exists():
            return None
        return json.loads(_RECOMMENDATION_PATH.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.debug("[WeightRecommender] 추천 결과 로드 실패: %s", exc)
        return None
