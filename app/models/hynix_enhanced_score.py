"""
hynix_enhanced_score.py — 개선된 최종점수 (calculate_enhanced_hynix_prediction_score).

기존 하이닉스 예측점수(predict_hynix().confidence_score)를 base_prediction_score로
그대로 유지하고, 마이크론 실시간점수/하이닉스 기술점수/장중모멘텀점수를 가중합해
enhanced_score를 계산한다. 인버스 압력점수는 별도 산출해 함께 반환한다(가중합에는
포함하지 않음 — 최종 판단은 hynix_action_decider가 enhanced_score와
inverse_pressure_score를 함께 사용).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from datetime import datetime

from app.logger import logger

ROOT = Path(__file__).resolve().parent.parent.parent
_WEIGHTS_PATH = ROOT / "config" / "hynix_enhanced_weights.json"

_DEFAULT_WEIGHTS = {
    "base_prediction": 0.45,
    "existing_micron": 0.20,
    "hynix_technical": 0.25,
    "intraday_momentum": 0.10,
}


def _load_weights() -> dict:
    try:
        from app.services.hynix_weight_manager import get_active_weights

        return get_active_weights()
    except Exception as exc:
        logger.debug("[EnhancedScore] 활성 가중치 로드 실패, config 기본값 사용: %s", exc)
    try:
        if _WEIGHTS_PATH.exists():
            data = json.loads(_WEIGHTS_PATH.read_text(encoding="utf-8"))
            return {**_DEFAULT_WEIGHTS, **(data.get("weights") or {})}
    except Exception as exc:
        logger.debug("[EnhancedScore] 가중치 로드 실패, 기본값 사용: %s", exc)
    return dict(_DEFAULT_WEIGHTS)


def calculate_enhanced_hynix_prediction_score(mode: Optional[str] = None) -> dict:
    """개선된 최종점수 산출. mode(mock/real/None)는 데이터 수집 계좌 컨텍스트."""
    from app.data_sources.auto_market_collector import collect_all
    from app.data_sources.hynix_inverse_collector import collect_inverse_current
    from app.features.hynix_auto_features import build_auto_features
    from app.models.hynix_predictor import predict_hynix
    from app.models.hynix_micron_realtime_score import calculate_existing_micron_score
    from app.models.hynix_technical_score import calculate_hynix_technical_score
    from app.models.hynix_intraday_momentum_score import calculate_intraday_momentum_score
    from app.models.hynix_inverse_pressure_score import calculate_inverse_pressure_score

    warnings: list[str] = []
    data_valid = {"base_prediction": True, "existing_micron": True, "hynix_technical": True, "intraday_momentum": True}

    market_data = collect_all(mode=mode)
    auto_features = build_auto_features(market_data)
    micron_features = auto_features.get("micron_features", {})
    predictor_kwargs = auto_features.get("predictor_kwargs", {})

    hynix_data = market_data.get("hynix", {}) or {}
    hynix_minute = market_data.get("hynix_minute", {}) or {}
    df_daily = hynix_data.get("df_daily")
    df_1min = hynix_minute.get("df_1min")
    hynix_current_price = hynix_data.get("current_price")
    kospilab_result = market_data.get("kospilab", {}) or {}
    investor_flow_raw = market_data.get("investor_flow", {}) or {}
    investor_flow = investor_flow_raw if (
        investor_flow_raw.get("foreign_net_buy") is not None or investor_flow_raw.get("institution_net_buy") is not None
    ) else None

    # ── base_prediction_score (기존 예측점수, 그대로 재사용) ────────────────
    try:
        base_prediction = predict_hynix(micron_features=micron_features, **predictor_kwargs)
        base_prediction_score = base_prediction.get("confidence_score")
    except Exception as exc:
        warnings.append(f"predict_hynix 실패: {exc}")
        base_prediction = {}
        base_prediction_score = None
    if base_prediction_score is None:
        base_prediction_score = 50.0
        data_valid["base_prediction"] = False
        warnings.append("base_prediction_score 계산 불가 — 중립값(50) 사용")

    # ── existing_micron_score ────────────────────────────────────────────────
    try:
        micron_result = calculate_existing_micron_score(mode=mode)
    except Exception as exc:
        warnings.append(f"existing_micron_score 계산 실패: {exc}")
        micron_result = {"existing_micron_score": 50.0, "warnings": [str(exc)]}
        data_valid["existing_micron"] = False

    # ── hynix_technical_score ─────────────────────────────────────────────────
    try:
        tech_result = calculate_hynix_technical_score(df_daily, df_1min)
    except Exception as exc:
        warnings.append(f"hynix_technical_score 계산 실패: {exc}")
        tech_result = {"hynix_technical_score": 50.0, "reason_top5": [], "warnings": [str(exc)], "detail": {}}
        data_valid["hynix_technical"] = False

    # ── intraday_momentum_score ───────────────────────────────────────────────
    try:
        momentum_result = calculate_intraday_momentum_score(df_1min)
    except Exception as exc:
        warnings.append(f"intraday_momentum_score 계산 실패: {exc}")
        momentum_result = {"intraday_momentum_score": 50.0, "reason_top5": [], "warnings": [str(exc)], "detail": {}}
        data_valid["intraday_momentum"] = False

    # ── inverse_pressure_score ────────────────────────────────────────────────
    try:
        inverse_result = calculate_inverse_pressure_score(
            tech_result=tech_result, momentum_result=momentum_result, micron_result=micron_result,
            kospilab_result=kospilab_result, df_1min=df_1min, current_price=hynix_current_price,
            investor_flow=investor_flow,
        )
    except Exception as exc:
        warnings.append(f"inverse_pressure_score 계산 실패: {exc}")
        inverse_result = {"inverse_pressure_score": 50.0, "inverse_pressure_tier": "HOLD", "reason_top5": [], "warnings": [str(exc)]}

    # ── 인버스(0197X0) 현재가 ────────────────────────────────────────────────
    try:
        inverse_price_result = collect_inverse_current(mode=mode)
    except Exception as exc:
        warnings.append(f"인버스 현재가 수집 실패: {exc}")
        inverse_price_result = {"current_price": None, "stale": True, "error": str(exc)}

    weights = _load_weights()
    existing_micron_score = float(micron_result.get("existing_micron_score", 50.0))
    hynix_technical_score = float(tech_result.get("hynix_technical_score", 50.0))
    intraday_momentum_score = float(momentum_result.get("intraday_momentum_score", 50.0))
    inverse_pressure_score = float(inverse_result.get("inverse_pressure_score", 50.0))

    enhanced_score = (
        base_prediction_score * weights["base_prediction"]
        + existing_micron_score * weights["existing_micron"]
        + hynix_technical_score * weights["hynix_technical"]
        + intraday_momentum_score * weights["intraday_momentum"]
    )
    enhanced_score = round(max(0.0, min(100.0, enhanced_score)), 2)

    candidates: list[tuple[float, str]] = [
        (abs(base_prediction_score - 50) * weights["base_prediction"], f"기존 예측점수 {base_prediction_score:.1f}/100"),
        (abs(existing_micron_score - 50) * weights["existing_micron"], f"마이크론 실시간점수 {existing_micron_score:.1f}/100 ({micron_result.get('source')})"),
        (abs(hynix_technical_score - 50) * weights["hynix_technical"], f"하이닉스 기술점수 {hynix_technical_score:.1f}/100"),
        (abs(intraday_momentum_score - 50) * weights["intraday_momentum"], f"장중 모멘텀점수 {intraday_momentum_score:.1f}/100"),
    ]
    if tech_result.get("reason_top5"):
        candidates.append((abs(hynix_technical_score - 50) * weights["hynix_technical"] * 0.9, tech_result["reason_top5"][0]))
    if momentum_result.get("reason_top5"):
        candidates.append((abs(intraday_momentum_score - 50) * weights["intraday_momentum"] * 0.9, momentum_result["reason_top5"][0]))
    candidates.sort(key=lambda x: x[0], reverse=True)
    reason_top5 = [desc for _, desc in candidates[:5]]

    return {
        "base_prediction_score": round(base_prediction_score, 2),
        "existing_micron_score": round(existing_micron_score, 2),
        "hynix_technical_score": round(hynix_technical_score, 2),
        "intraday_momentum_score": round(intraday_momentum_score, 2),
        "inverse_pressure_score": round(inverse_pressure_score, 2),
        "inverse_pressure_tier": inverse_result.get("inverse_pressure_tier"),
        "enhanced_score": enhanced_score,
        "reason_top5": reason_top5,
        "data_valid": data_valid,
        "warnings": warnings + tech_result.get("warnings", []) + momentum_result.get("warnings", []) + micron_result.get("warnings", []) + inverse_result.get("warnings", []),
        "hynix_current_price": hynix_current_price,
        "hynix_prev_close": hynix_data.get("prev_close"),
        "inverse_current_price": inverse_price_result.get("current_price"),
        "inverse_price_stale": bool(inverse_price_result.get("stale")),
        "micron_detail": micron_result,
        "tech_detail": tech_result,
        "momentum_detail": momentum_result,
        "inverse_detail": inverse_result,
        "market_data": market_data,
        "computed_at": datetime.now().isoformat(),
    }
