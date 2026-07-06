#!/usr/bin/env python
"""CLI script to train the gap-trade ML model from historical data.

Usage:
    python scripts/run_train_model.py [--features PATH] [--labels PATH]

The script scans data/features/ and data/labels/ for matching YYYYMMDD CSV
files, merges them, and trains a RandomForest classifier.

Outputs:
    models/gap_model.pkl
    models/feature_importance.csv
    reports/train_report_YYYYMMDD.txt
"""

import sys
import argparse
import glob
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
import os
os.chdir(PROJECT_ROOT)

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import pandas as pd
from app.ml.train_model import ModelTrainer
from app.config import get_config


def _collect_all_csvs(directory: Path, suffix: str) -> pd.DataFrame:
    """Concatenate all *_suffix.csv files in directory into one DataFrame."""
    pattern = str(directory / f"*_{suffix}.csv")
    files = sorted(glob.glob(pattern))
    if not files:
        return pd.DataFrame()
    frames = []
    for f in files:
        try:
            df = pd.read_csv(f, encoding="utf-8-sig")
            frames.append(df)
        except Exception as exc:
            print(f"  [warn] 파일 읽기 실패: {f} — {exc}")
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)


def main():
    parser = argparse.ArgumentParser(description="Train the gap-trade ML model")
    parser.add_argument(
        "--features",
        default=None,
        help="Path to a single features CSV (default: all data/features/*_features.csv)",
    )
    parser.add_argument(
        "--labels",
        default=None,
        help="Path to a single labels CSV (default: all data/labels/*_labels.csv)",
    )
    args = parser.parse_args()

    cfg = get_config()
    trainer = ModelTrainer()

    # --- Load features ---
    if args.features:
        print(f"[train] 피처 파일 로드: {args.features}")
        features_df = pd.read_csv(args.features, encoding="utf-8-sig")
    else:
        features_dir = PROJECT_ROOT / "data" / "features"
        print(f"[train] 피처 디렉토리 스캔: {features_dir}")
        features_df = _collect_all_csvs(features_dir, "features")

    if features_df.empty:
        print("[train] 오류: 피처 데이터가 없습니다. 먼저 run_collect_data.py를 실행하세요.")
        sys.exit(1)

    print(f"[train] 피처 로드 완료: {len(features_df)}행, {len(features_df.columns)}열")

    # --- Load labels ---
    if args.labels:
        print(f"[train] 레이블 파일 로드: {args.labels}")
        labels_df = pd.read_csv(args.labels, encoding="utf-8-sig")
    else:
        labels_dir = PROJECT_ROOT / "data" / "labels"
        print(f"[train] 레이블 디렉토리 스캔: {labels_dir}")
        labels_df = _collect_all_csvs(labels_dir, "labels")

    if labels_df.empty:
        print("[train] 오류: 레이블 데이터가 없습니다. data/labels/ 디렉토리에 레이블 CSV가 필요합니다.")
        sys.exit(1)

    print(f"[train] 레이블 로드 완료: {len(labels_df)}행")

    # --- Check trainability ---
    if not trainer.can_train(features_df, labels_df):
        min_rows = cfg.ml.get("min_training_rows", 500)
        print(
            f"[train] 학습 데이터 부족: 유효한 레이블 행이 {min_rows}개 미만입니다. "
            "더 많은 historical 데이터를 수집한 후 다시 시도하세요."
        )
        sys.exit(1)

    # --- Train ---
    print("[train] 모델 학습 시작 ...")
    result = trainer.train(features_df, labels_df)

    if not result.get("success"):
        reason = result.get("reason", "unknown")
        print(f"[train] 학습 실패: {reason}")
        sys.exit(1)

    # --- Print results ---
    accuracy = result["accuracy"]
    n_train = result["n_train"]
    n_test = result["n_test"]
    fi = result.get("feature_importance", {})

    print("\n" + "=" * 50)
    print("[train] 학습 완료")
    print("=" * 50)
    print(f"  정확도 (accuracy) : {accuracy:.4f} ({accuracy * 100:.2f}%)")
    print(f"  학습 샘플 수      : {n_train}")
    print(f"  테스트 샘플 수    : {n_test}")
    print(f"  모델 저장 경로    : {cfg.ml.get('model_path', 'models/gap_model.pkl')}")

    if fi:
        print("\n[train] 피처 중요도 (상위 10개):")
        sorted_fi = sorted(fi.items(), key=lambda kv: kv[1], reverse=True)[:10]
        for feat, imp in sorted_fi:
            bar = "#" * int(imp * 50)
            print(f"  {feat:<40s} {imp:.4f}  {bar}")

    print("\n[train] 완료.")


if __name__ == "__main__":
    main()
