"""train_hynix_models.py — 수집된 과거 데이터로 horizon별 ML 모델을 학습한다.

사용법:
    python scripts/train_hynix_models.py [--fresh]

기본은 로컬 캐시(data/historical/raw/*.parquet, collect_historical_data.py로
미리 수집)만 사용해 빠르게 재학습한다. --fresh를 주면 전체를 다시 수집한다
(느림, 실 API 호출).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from app.ml.historical_data_loader import collect_all_historical, load_all_from_cache  # noqa: E402
from app.ml.hynix_ml_trainer import load_training_config, train_all_models  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="SK하이닉스 ML 모델 학습")
    parser.add_argument("--fresh", action="store_true", help="캐시 대신 전체를 다시 수집한다")
    parser.add_argument("--lookback-days", type=int, default=365)
    args = parser.parse_args()

    cfg = load_training_config()
    if args.fresh:
        print("실 API로 전체 데이터를 다시 수집합니다...")
        historical_data = collect_all_historical(lookback_days=args.lookback_days)
    else:
        print("로컬 캐시로 학습합니다(빠름, 최신성은 마지막 수집 시점 기준)...")
        historical_data = load_all_from_cache()

    print("모델 학습 시작...")
    results = train_all_models(historical_data, training_cfg=cfg)

    for horizon, info in results["horizons"].items():
        error = info.get("error")
        if error:
            print(f"[{horizon}] 학습 실패/생략: {error}")
        else:
            reg = info.get("regressor_metrics", {})
            clf = info.get("direction_metrics", {})
            print(
                f"[{horizon}] backend={info.get('backend')} n={info.get('n_samples')} "
                f"below_min_samples={info.get('below_min_samples')} "
                f"reg_mae={reg.get('mae')} dir_acc={clf.get('accuracy')}"
            )
    if results["warnings"]:
        print("경고:")
        for w in results["warnings"]:
            print(f"  - {w}")


if __name__ == "__main__":
    main()
