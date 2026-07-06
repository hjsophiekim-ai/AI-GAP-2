#!/usr/bin/env python
"""CLI script: full pipeline collect -> features -> predict -> candidate50 -> top15.

Usage:
    python scripts/run_select_top15.py [--date YYYYMMDD] [--no-save]

Outputs:
    data/candidates/YYYYMMDD_candidate50.csv
    data/selected/YYYYMMDD_top15.csv
    Prints formatted top-15 table to console.
"""

import sys
import argparse
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
import os
os.chdir(PROJECT_ROOT)

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import pandas as pd
from app.data.data_collector import DataCollector
from app.features.feature_builder import FeatureBuilder
from app.ml.predict_model import ModelPredictor
from app.strategy.candidate_generator import CandidateGenerator
from app.strategy.top15_selector import Top15Selector
from app.utils.time_utils import today_str


def _fmt_trade_value(val: float) -> str:
    if val >= 1_000_000_000_000:
        return f"{val / 1_000_000_000_000:.1f}조"
    if val >= 1_000_000_000:
        return f"{val / 1_000_000_000:.1f}B"
    if val >= 100_000_000:
        return f"{val / 100_000_000:.0f}억"
    return f"{val:,.0f}"


def main():
    parser = argparse.ArgumentParser(description="Run full pipeline and print top-15 candidates")
    parser.add_argument(
        "--date",
        default=today_str(),
        help="Target date in YYYYMMDD format (default: today)",
    )
    parser.add_argument(
        "--no-save",
        action="store_true",
        help="Skip saving intermediate CSV files",
    )
    args = parser.parse_args()

    date_str = args.date
    save = not args.no_save

    print(f"[top15] 날짜: {date_str}")

    # 1. Collect
    print("[top15] 1/5 데이터 수집 ...")
    collector = DataCollector()
    stocks = collector.collect_gap_candidates(date_str=date_str)
    print(f"       수집 완료: {len(stocks)}개 종목")

    # 2. Build features
    print("[top15] 2/5 피처 생성 ...")
    builder = FeatureBuilder()
    features = builder.build_features(stocks)
    if save:
        feat_path = builder.save_features(features, date_str=date_str)
        print(f"       피처 저장: {feat_path}")
    print(f"       피처 완료: {len(features)}개")

    # 3. Predict (ML scoring, falls back to rule score if no model)
    print("[top15] 3/5 ML 예측 ...")
    predictor = ModelPredictor()
    predictions = predictor.predict(features)
    if save:
        pred_path = predictor.save_predictions(predictions, date_str=date_str)
        print(f"       예측 저장: {pred_path}")
    print(f"       예측 완료: {len(predictions)}개")

    # 4. Generate candidate50
    print("[top15] 4/5 후보 50개 생성 ...")
    generator = CandidateGenerator()
    candidates = generator.generate(stocks, predictions=predictions)
    if save:
        cand_path = generator.save_candidates(candidates, date_str=date_str)
        print(f"       후보 저장: {cand_path} ({len(candidates)}개)")
    print(f"       후보 완료: {len(candidates)}개")

    # 5. Select top15
    print("[top15] 5/5 Top-15 선정 ...")
    selector = Top15Selector()
    top15 = selector.select(candidates)
    if save:
        top15_path = selector.save_top15(top15, date_str=date_str)
        print(f"       Top15 저장: {top15_path}")
    print(f"       Top15 완료: {len(top15)}개\n")

    # Print table
    if not top15:
        print("[top15] 선정된 종목이 없습니다.")
        return

    rows = []
    for c in top15:
        rows.append({
            "순위": c.rank,
            "코드": c.symbol,
            "종목명": c.name,
            "현재가": f"{c.current_price:,.0f}",
            "갭률(%)": f"{c.gap_rate:.2f}",
            "시가대비(%)": f"{c.open_to_current_rate:.2f}",
            "거래대금": _fmt_trade_value(c.trade_value),
            "규칙점수": f"{c.rule_score:.2f}",
            "최종점수": f"{c.final_score:.4f}",
        })

    df = pd.DataFrame(rows)
    print("=" * 90)
    print(f"  AI-GAP Top-15 선정 결과 ({date_str})")
    print("=" * 90)
    print(df.to_string(index=False))
    print("=" * 90)
    print(f"  총 {len(top15)}개 종목 선정")
    print("[top15] 완료.")


if __name__ == "__main__":
    main()
