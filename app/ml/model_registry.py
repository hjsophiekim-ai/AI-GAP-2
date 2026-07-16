"""model_registry.py — 학습된 horizon별 모델(회귀/분류) pkl + 메타데이터 저장/조회.

레지스트리 자체는 JSON 파일(registry.json)이며, 실제 모델 객체는 joblib으로
별도 .pkl에 저장한다. 파일이 없거나 손상되어도 예외를 던지지 않고 None을
반환한다 — 호출부(hynix_ml_predictor)가 이를 "ML 모델 없음, Rule로 대체"로
처리한다.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

try:
    from app.logger import logger
except ImportError:
    logger = logging.getLogger(__name__)

import joblib

from app.utils.data_paths import HISTORICAL_DIR

ROOT = Path(__file__).resolve().parent.parent.parent
MODELS_DIR = HISTORICAL_DIR / "models"
REGISTRY_PATH = MODELS_DIR / "registry.json"


def _load_registry() -> dict:
    try:
        if REGISTRY_PATH.exists():
            return json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.debug("[ModelRegistry] registry 읽기 실패: %s", exc)
    return {}


def _save_registry(registry: dict) -> None:
    try:
        MODELS_DIR.mkdir(parents=True, exist_ok=True)
        REGISTRY_PATH.write_text(json.dumps(registry, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    except Exception as exc:
        logger.warning("[ModelRegistry] registry 저장 실패: %s", exc)


def _key(horizon: str, task: str) -> str:
    return f"{horizon}_{task}"


def save_model(horizon: str, task: str, model, metadata: dict) -> str:
    """task: "regressor" | "direction". Returns saved model file path."""
    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    suffix = "regressor" if task == "regressor" else "direction"
    filename = f"model_{horizon}_{suffix}.pkl"
    path = MODELS_DIR / filename
    try:
        joblib.dump(model, path)
    except Exception as exc:
        logger.warning("[ModelRegistry] %s 저장 실패: %s", filename, exc)
        return ""

    registry = _load_registry()
    registry[_key(horizon, task)] = {
        **metadata, "path": str(path), "saved_at": datetime.now().isoformat(timespec="seconds"),
    }
    _save_registry(registry)
    return str(path)


def load_model(horizon: str, task: str):
    """Returns (model|None, metadata|None). 실패해도 예외 없음."""
    registry = _load_registry()
    entry = registry.get(_key(horizon, task))
    if not entry:
        return None, None
    path = Path(entry.get("path", ""))
    if not path.exists():
        logger.debug("[ModelRegistry] 모델 파일 없음: %s", path)
        return None, entry
    try:
        model = joblib.load(path)
        return model, entry
    except Exception as exc:
        logger.warning("[ModelRegistry] %s 로드 실패: %s", path, exc)
        return None, entry


def get_metadata(horizon: str, task: str) -> Optional[dict]:
    return _load_registry().get(_key(horizon, task))


def list_registry() -> dict:
    return _load_registry()


def has_trained_models() -> bool:
    return bool(_load_registry())
