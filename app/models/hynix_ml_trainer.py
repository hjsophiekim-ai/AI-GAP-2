"""
hynix_ml_trainer.py — SK하이닉스 예측 머신러닝 확장 모듈 (구조 준비).

현재 버전은 규칙 기반 모델을 사용합니다.
향후 LightGBM/XGBoost로 교체할 수 있도록 인터페이스를 구조화합니다.

사용법 (미래):
    trainer = HynixMLTrainer()
    trainer.build_dataset(predictions)
    trainer.train()
    trainer.predict(features)
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

import pandas as pd

_ROOT = Path(__file__).resolve().parent.parent.parent
_MODEL_DIR = _ROOT / "data" / "models"

# feature 컬럼 목록 (학습 시 사용할 feature 순서)
FEATURE_COLUMNS = [
    "micron_premarket_return",
    "micron_premarket_open_to_now",
    "micron_premarket_high_to_now",
    "micron_premarket_low_to_now",
    "micron_premarket_30m_momentum",
    "micron_premarket_60m_momentum",
    "micron_premarket_vwap",
    "micron_premarket_volume_change",
    "micron_regular_return",
    "micron_aftermarket_return",
    "micron_session_strength_score",
    "kospilab_expected_return_pct",
    "sox_return_pct",
    "nvda_return_pct",
    "qqq_return_pct",
    "usd_krw_change_pct",
    "hynix_prev_return_pct",
    "hynix_return_3d_pct",
    "hynix_return_5d_pct",
    "hynix_return_10d_pct",
    "hynix_volume_change_pct",
]

# 예측 target 컬럼
TARGET_COLUMNS = [
    "today_return_pct",
    "tomorrow_return_pct",
    "day3_return_pct",
]


class HynixMLTrainer:
    """
    SK하이닉스 예측 ML 트레이너.
    현재는 인터페이스만 구조화하며, 실제 모델 학습은 미구현.
    """

    def __init__(self) -> None:
        self.model: Any = None
        self.model_path = _MODEL_DIR / "hynix_lgbm.pkl"
        self.feature_importance: Optional[dict] = None

    def build_dataset(self, prediction_records: list[dict]) -> pd.DataFrame:
        """
        예측 이력 레코드에서 feature matrix와 target vector 생성.

        Parameters
        ----------
        prediction_records : JSONL 파일에서 로드한 예측 상세 목록

        Returns
        -------
        DataFrame
            feature 컬럼 + target 컬럼 포함
        """
        rows = []
        for rec in prediction_records:
            micron = rec.get("micron_features", {})
            other  = rec.get("other_inputs", {})
            kos    = rec.get("kospilab_inputs", {})
            pred   = rec.get("prediction", {})

            row: dict[str, Any] = {}
            for col in FEATURE_COLUMNS:
                if col in micron:
                    row[col] = micron.get(col)
                elif col in other:
                    row[col] = other.get(col)
                elif col in kos:
                    row[col] = kos.get(col)
                else:
                    row[col] = None

            for t in TARGET_COLUMNS:
                row[t] = pred.get(t)

            # 실제 결과가 있으면 실제값으로 덮어씀 (학습 레이블)
            actual_close = pred.get("actual_close")
            actual_open  = pred.get("actual_open")
            if actual_close and actual_open:
                try:
                    row["today_return_pct"] = (
                        (float(actual_close) / float(actual_open) - 1) * 100
                    )
                except Exception:
                    pass

            rows.append(row)

        return pd.DataFrame(rows)

    def train(self, df: pd.DataFrame) -> None:
        """
        모델 학습 (미구현 — LightGBM/XGBoost 추가 시 이 메서드를 채움).

        Parameters
        ----------
        df : build_dataset()의 반환값
        """
        # TODO: feature/target 분리 및 train/test split
        # X = df[FEATURE_COLUMNS].fillna(0)
        # y = df["today_return_pct"].fillna(0)
        # X_train, X_test, y_train, y_test = train_test_split(X, y, ...)
        # model = lgb.LGBMRegressor(...)
        # model.fit(X_train, y_train)
        # self.model = model
        # self._save_model()
        raise NotImplementedError(
            "ML 학습은 아직 미구현입니다. "
            "LightGBM/XGBoost 패키지 설치 후 구현하세요."
        )

    def predict(self, features: dict) -> Optional[dict]:
        """
        ML 모델로 예측 (미구현 — 모델 학습 후 활성화).

        Parameters
        ----------
        features : feature 이름 → 값 dict

        Returns
        -------
        dict | None
            {today_return_pct, tomorrow_return_pct, day3_return_pct}
        """
        if self.model is None:
            return None
        # TODO: self.model.predict(...)
        raise NotImplementedError("ML 예측은 아직 미구현입니다.")

    def _save_model(self) -> None:
        """학습된 모델 저장 (pickle)."""
        import pickle
        _MODEL_DIR.mkdir(parents=True, exist_ok=True)
        with open(self.model_path, "wb") as f:
            pickle.dump(self.model, f)

    def load_model(self) -> bool:
        """저장된 모델 로드."""
        if not self.model_path.exists():
            return False
        try:
            import pickle
            with open(self.model_path, "rb") as f:
                self.model = pickle.load(f)
            return True
        except Exception:
            return False
