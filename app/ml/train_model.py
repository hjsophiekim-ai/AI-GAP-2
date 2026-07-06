"""Train a RandomForest classifier to predict good gap trades."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import joblib  # noqa: F401 — available for callers
import pandas as pd
from sklearn.ensemble import RandomForestClassifier
from sklearn.metrics import accuracy_score
from sklearn.model_selection import train_test_split

from app.config import get_config
from app.logger import logger
from app.ml.model_store import ModelStore


class ModelTrainer:
    """Trains and evaluates the gap-trade classification model."""

    TARGET_COL = "label_good_trade"
    JOIN_KEYS = ["symbol", "date"]

    def __init__(self) -> None:
        cfg = get_config()
        ml_cfg = cfg.ml
        self._min_training_rows: int = ml_cfg.get("min_training_rows", 500)
        self._model_path: str = ml_cfg.get("model_path", "models/gap_model.pkl")
        self._fi_path: str = ml_cfg.get(
            "feature_importance_path", "models/feature_importance.csv"
        )
        self._store = ModelStore()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def can_train(self, features_df: pd.DataFrame, labels_df: pd.DataFrame) -> bool:
        """Return ``True`` when merged data has enough rows with valid labels."""
        merged = self._merge(features_df, labels_df)
        valid = merged[merged[self.TARGET_COL].notna()]
        return len(valid) >= self._min_training_rows

    def train(
        self, features_df: pd.DataFrame, labels_df: pd.DataFrame
    ) -> dict:
        """Train model and return a result dict.

        Returns ``{"success": False, "reason": "insufficient_data"}`` when there
        are fewer than *min_training_rows* labelled rows available.

        On success returns::

            {
                "success": True,
                "accuracy": float,
                "n_train": int,
                "n_test": int,
                "feature_importance": dict,
            }
        """
        merged = self._merge(features_df, labels_df)
        valid = merged[merged[self.TARGET_COL].notna()].copy()

        if len(valid) < self._min_training_rows:
            logger.warning(
                f"[ModelTrainer] insufficient data: {len(valid)} rows "
                f"(need {self._min_training_rows})"
            )
            return {"success": False, "reason": "insufficient_data"}

        feature_cols = [
            c for c in valid.columns if c not in self.JOIN_KEYS + [self.TARGET_COL]
        ]
        X = valid[feature_cols]
        y = valid[self.TARGET_COL].astype(int)

        X_train, X_test, y_train, y_test = train_test_split(
            X, y, test_size=0.2, random_state=42
        )

        clf = RandomForestClassifier(n_estimators=100, random_state=42)
        clf.fit(X_train, y_train)

        y_pred = clf.predict(X_test)
        accuracy = float(accuracy_score(y_test, y_pred))

        feature_importance: dict = dict(
            zip(feature_cols, clf.feature_importances_.tolist())
        )

        # Persist artefacts
        self._store.save_model(clf, self._model_path)
        self._store.save_feature_importance(feature_importance, self._fi_path)
        self._save_report(accuracy, len(X_train), len(X_test), feature_importance)

        logger.info(
            f"[ModelTrainer] training complete — accuracy={accuracy:.4f} "
            f"n_train={len(X_train)} n_test={len(X_test)}"
        )

        return {
            "success": True,
            "accuracy": accuracy,
            "n_train": len(X_train),
            "n_test": len(X_test),
            "feature_importance": feature_importance,
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _merge(
        self, features_df: pd.DataFrame, labels_df: pd.DataFrame
    ) -> pd.DataFrame:
        """Inner-join features and labels on symbol + date."""
        return features_df.merge(labels_df, on=self.JOIN_KEYS, how="inner")

    def _save_report(
        self,
        accuracy: float,
        n_train: int,
        n_test: int,
        feature_importance: dict,
    ) -> None:
        """Write a human-readable training report to *reports/*."""
        date_str = datetime.now().strftime("%Y%m%d")
        report_dir = Path("reports")
        report_dir.mkdir(parents=True, exist_ok=True)
        report_path = report_dir / f"train_report_{date_str}.txt"

        lines = [
            f"Training report — {datetime.now().isoformat()}",
            f"accuracy : {accuracy:.6f}",
            f"n_train  : {n_train}",
            f"n_test   : {n_test}",
            "",
            "Feature importance (sorted desc):",
        ]
        sorted_fi = sorted(feature_importance.items(), key=lambda kv: kv[1], reverse=True)
        for feat, imp in sorted_fi:
            lines.append(f"  {feat:<40s} {imp:.6f}")

        report_path.write_text("\n".join(lines), encoding="utf-8")
        logger.info(f"[ModelTrainer] report saved → {report_path}")
