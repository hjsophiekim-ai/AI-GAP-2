"""Persistence helpers for trained ML models and feature importance."""

from __future__ import annotations

import csv
import os
from pathlib import Path

import joblib

from app.logger import logger


class ModelStore:
    """Save and load scikit-learn models and associated artefacts."""

    # ------------------------------------------------------------------
    # Model persistence
    # ------------------------------------------------------------------

    def save_model(self, model, path: str = "models/gap_model.pkl") -> None:
        """Persist *model* to *path* using joblib."""
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        joblib.dump(model, path)
        logger.info(f"[ModelStore] model saved → {path}")

    def load_model(self, path: str = "models/gap_model.pkl") -> object | None:
        """Load and return the model at *path*.

        Returns ``None`` (does **not** raise) when the file is missing.
        """
        if not os.path.exists(path):
            logger.warning(f"[ModelStore] model file not found: {path}")
            return None
        model = joblib.load(path)
        logger.info(f"[ModelStore] model loaded ← {path}")
        return model

    def model_exists(self, path: str = "models/gap_model.pkl") -> bool:
        """Return ``True`` if a saved model file exists at *path*."""
        return os.path.isfile(path)

    # ------------------------------------------------------------------
    # Feature importance
    # ------------------------------------------------------------------

    def save_feature_importance(
        self,
        importances: dict,
        path: str = "models/feature_importance.csv",
    ) -> None:
        """Save *importances* dict as a CSV with columns ``feature, importance``."""
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            writer.writerow(["feature", "importance"])
            for feature, importance in importances.items():
                writer.writerow([feature, importance])
        logger.info(f"[ModelStore] feature importance saved → {path}")
