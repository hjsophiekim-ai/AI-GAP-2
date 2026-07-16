"""
hynix_weight_manager.py — 가중치 조회/적용/초기화(사람 승인 후 반영).

추천 가중치는 사용자가 "적용" 버튼을 눌렀을 때만 `data/state/hynix_model_weights.json`에
반영된다. 이 파일이 있으면 `hynix_enhanced_score`의 기본 가중치(config/hynix_enhanced_weights.json)
보다 우선 적용된다. real 자동매매 중 자동 변경 경로는 존재하지 않으며(수동 버튼만 반영),
mock 모드에서는 `weight_auto_apply_enabled` 플래그로 실험적 자동 적용을 켤 수 있다.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Optional

from app.logger import logger
from app.utils.data_paths import STATE_DIR

ROOT = Path(__file__).resolve().parent.parent.parent
_DEFAULT_WEIGHTS_PATH = ROOT / "config" / "hynix_enhanced_weights.json"
_ACTIVE_WEIGHTS_PATH = STATE_DIR / "hynix_model_weights.json"

_FALLBACK_DEFAULTS = {
    "base_prediction": 0.45, "existing_micron": 0.20, "hynix_technical": 0.25, "intraday_momentum": 0.10,
}

_WEIGHT_KEYS = ["base_prediction", "existing_micron", "hynix_technical", "intraday_momentum"]


def get_default_weights() -> dict:
    try:
        if _DEFAULT_WEIGHTS_PATH.exists():
            data = json.loads(_DEFAULT_WEIGHTS_PATH.read_text(encoding="utf-8"))
            return {**_FALLBACK_DEFAULTS, **(data.get("weights") or {})}
    except Exception as exc:
        logger.debug("[WeightManager] 기본 가중치 로드 실패: %s", exc)
    return dict(_FALLBACK_DEFAULTS)


def get_active_weights() -> dict:
    """현재 실제 사용 중인 가중치. data/state/hynix_model_weights.json이 있으면 우선."""
    try:
        if _ACTIVE_WEIGHTS_PATH.exists():
            data = json.loads(_ACTIVE_WEIGHTS_PATH.read_text(encoding="utf-8"))
            weights = data.get("weights") or data
            merged = {**get_default_weights(), **{k: v for k, v in weights.items() if k in _WEIGHT_KEYS}}
            return merged
    except Exception as exc:
        logger.debug("[WeightManager] 활성 가중치 로드 실패, 기본값 사용: %s", exc)
    return get_default_weights()


def _write_active_weights(weights: dict, source: str) -> dict:
    total = sum(weights.get(k, 0.0) for k in _WEIGHT_KEYS) or 1.0
    normalized = {k: round(weights.get(k, 0.0) / total, 4) for k in _WEIGHT_KEYS}
    payload = {"weights": normalized, "source": source, "applied_at": datetime.now().isoformat()}
    try:
        _ACTIVE_WEIGHTS_PATH.parent.mkdir(parents=True, exist_ok=True)
        _ACTIVE_WEIGHTS_PATH.write_text(json.dumps(payload, ensure_ascii=False, default=str, indent=2), encoding="utf-8")
    except Exception as exc:
        logger.error("[WeightManager] 가중치 저장 실패: %s", exc)
        return {"success": False, "message": str(exc)}
    return {"success": True, "weights": normalized, "source": source}


def apply_recommended_weights() -> dict:
    """추천 가중치를 실제 활성 가중치로 반영(사용자의 명시적 '적용' 버튼 클릭 전용)."""
    from app.services.hynix_weight_recommender import load_recommendation

    rec = load_recommendation()
    if not rec or rec.get("skipped") or not rec.get("recommended_weights"):
        return {"success": False, "message": "적용 가능한 추천 가중치가 없습니다(샘플 부족 또는 미생성)."}
    return _write_active_weights(rec["recommended_weights"], source="recommended")


def reset_weights_to_default() -> dict:
    """기본 가중치(config/hynix_enhanced_weights.json)로 되돌린다."""
    return _write_active_weights(get_default_weights(), source="default_reset")


def maybe_auto_apply_in_mock(mode: str, auto_apply_enabled: bool) -> Optional[dict]:
    """mock 모드 + 실험용 자동 적용 옵션이 켜져 있을 때만 자동 반영. real에서는 절대 호출/동작하지 않음."""
    if mode != "mock" or not auto_apply_enabled:
        return None
    result = apply_recommended_weights()
    if result.get("success"):
        logger.info("[WeightManager] mock 모드 실험용 자동 적용 수행: %s", result.get("weights"))
    return result
