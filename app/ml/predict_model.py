"""
predict_model.py — ML prediction for gap-up candidates.

Uses a trained scikit-learn model (if available) to score candidates,
falling back to normalised rule_score when no model is present or
use_model=False is configured.
"""

from __future__ import annotations

import csv
import os
from datetime import datetime
from pathlib import Path
from typing import List, Optional

import numpy as np

from app.config import get_config
from app.logger import logger
from app.models import StockFeatures

# ModelStore is optional — only imported when a model is actually needed.
# This avoids hard-failing at import time if the module does not yet exist.
try:
    from app.ml.model_store import ModelStore
    _MODEL_STORE_AVAILABLE = True
except ImportError:
    _MODEL_STORE_AVAILABLE = False
    logger.warning("app.ml.model_store not found — ML model will not be loaded.")

# Feature names used by _get_feature_vector, kept in a single authoritative list.
_FEATURE_NAMES: List[str] = [
    "gap_rate",
    "open_to_current_rate",
    "high_from_open_rate",
    "low_from_open_rate",
    "current_from_high_rate",
    "trade_value_score",
    "volume_score",
    "gap_score",
    "price_strength_score",
    "high_break_score",
    "volatility_score",
    "liquidity_score",
    "risk_penalty",
    "total_rule_score",
]

_ROOT = Path(__file__).parent.parent.parent  # project root


class ModelPredictor:
    """Generates buy-candidate scores by combining an ML model with rule scores."""

    def __init__(self, cfg=None) -> None:
        self.cfg = cfg or get_config()
        self._model = None          # lazy-loaded trained model object
        self._model_loaded = False  # True once a load attempt has been made

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def predict(self, features: List[StockFeatures]) -> List[dict]:
        """Return a list of score dicts for each StockFeatures entry.

        Each dict contains:
            symbol      (str)
            name        (str)
            ml_score    (float)  – model probability or 0.0 when unavailable
            rule_score  (float)  – total_rule_score (raw, 0-100 scale)
            final_score (float)  – weighted combination, 0-1 scale
        """
        ml_cfg = self.cfg.ml
        use_model: bool = bool(ml_cfg.get("use_model", False))
        ml_weight: float = float(ml_cfg.get("ml_weight", 0.5))
        rule_weight: float = float(ml_cfg.get("rule_weight", 0.5))

        model = None
        if use_model:
            model = self._load_model()

        results: List[dict] = []

        if model is not None and features:
            # Build feature matrix in one shot for efficiency.
            X = np.array(
                [self._get_feature_vector(f) for f in features],
                dtype=np.float64,
            )
            try:
                ml_scores: np.ndarray = model.predict_proba(X)[:, 1]
            except Exception as exc:
                logger.warning("model.predict_proba failed: %s — falling back to rule_score", exc)
                ml_scores = np.zeros(len(features), dtype=np.float64)
                model = None  # treat as unavailable for final_score calc below
        else:
            ml_scores = np.zeros(len(features), dtype=np.float64)

        for i, feat in enumerate(features):
            raw_rule: float = float(feat.total_rule_score or 0.0)
            rule_score_norm: float = raw_rule / 100.0
            ml_score: float = float(ml_scores[i])

            if model is not None:
                final_score = ml_score * ml_weight + rule_score_norm * rule_weight
            else:
                final_score = rule_score_norm

            results.append(
                {
                    "symbol": feat.symbol,
                    "name": feat.name,
                    "ml_score": round(ml_score, 6),
                    "rule_score": round(raw_rule, 4),
                    "final_score": round(final_score, 6),
                }
            )

        logger.info(
            "predict: %d candidates scored (model=%s)",
            len(results),
            "yes" if model is not None else "no",
        )
        return results

    def save_predictions(
        self, predictions: List[dict], date_str: Optional[str] = None
    ) -> str:
        """Save predictions to data/predictions/YYYYMMDD_predictions.csv.

        Returns the absolute path of the written file.
        """
        if date_str is None:
            date_str = datetime.now().strftime("%Y%m%d")

        out_dir = _ROOT / "data" / "predictions"
        out_dir.mkdir(parents=True, exist_ok=True)

        out_path = out_dir / f"{date_str}_predictions.csv"

        fieldnames = ["symbol", "name", "ml_score", "rule_score", "final_score"]

        with open(out_path, "w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=fieldnames)
            writer.writeheader()
            for row in predictions:
                writer.writerow({k: row.get(k, "") for k in fieldnames})

        logger.info("Predictions saved → %s (%d rows)", out_path, len(predictions))
        return str(out_path)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_feature_vector(self, feat: StockFeatures) -> List[float]:
        """Extract numeric feature values in the canonical _FEATURE_NAMES order.

        None values are replaced with 0.0.
        """
        return [
            _safe_float(getattr(feat, name, None))
            for name in _FEATURE_NAMES
        ]

    def _load_model(self):
        """Lazy-load the trained model from the configured model_path.

        Returns the model object, or None if loading fails or is not configured.
        """
        if self._model_loaded:
            return self._model

        self._model_loaded = True  # mark so we only try once per instance

        if not _MODEL_STORE_AVAILABLE:
            logger.info("ModelStore unavailable — skipping model load.")
            return None

        ml_cfg = self.cfg.ml
        model_path_cfg: Optional[str] = ml_cfg.get("model_path")

        if not model_path_cfg:
            logger.info("cfg.ml.model_path not set — no model loaded.")
            return None

        # Resolve relative paths against project root.
        model_path = Path(model_path_cfg)
        if not model_path.is_absolute():
            model_path = _ROOT / model_path

        if not model_path.exists():
            logger.info("Model file not found at %s — skipping ML scoring.", model_path)
            return None

        try:
            store = ModelStore()
            model = store.load(str(model_path))
            self._model = model
            logger.info("ML model loaded from %s", model_path)
        except Exception as exc:
            logger.warning("Failed to load ML model from %s: %s", model_path, exc)
            self._model = None

        return self._model


# ------------------------------------------------------------------
# Module-level helpers
# ------------------------------------------------------------------

def _safe_float(value) -> float:
    """Convert value to float, returning 0.0 for None or non-numeric types."""
    if value is None:
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0
