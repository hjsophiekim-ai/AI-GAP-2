#!/usr/bin/env python
"""CLI script to collect gap-up candidates and save to CSV.

Usage:
    python scripts/run_collect_data.py [--date YYYYMMDD] [--out PATH]

Outputs:
    data/raw/YYYYMMDD_raw_stocks.csv
    data/features/YYYYMMDD_features.csv
"""

import sys
import argparse
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
import os
os.chdir(PROJECT_ROOT)

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.data.data_collector import DataCollector
from app.features.feature_builder import FeatureBuilder
from app.utils.time_utils import today_str
import pandas as pd


def main():
    parser = argparse.ArgumentParser(description="Collect gap-up stock data and build features")
    parser.add_argument(
        "--date",
        default=today_str(),
        help="Target date in YYYYMMDD format (default: today)",
    )
    parser.add_argument(
        "--out",
        default=None,
        help="Optional output CSV path for raw stocks (default: data/raw/YYYYMMDD_raw_stocks.csv)",
    )
    args = parser.parse_args()

    date_str = args.date

    print(f"[collect] 날짜: {date_str}")
    print("[collect] 데이터 수집 시작 ...")

    # 1. Collect gap candidates
    collector = DataCollector()
    stocks = collector.collect_gap_candidates(date_str=date_str)
    print(f"[collect] 수집 완료: {len(stocks)}개 종목")

    # 2. Save raw stocks to CSV
    raw_dir = PROJECT_ROOT / "data" / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)

    out_path = args.out or str(raw_dir / f"{date_str}_raw_stocks.csv")
    rows = [s.__dict__ for s in stocks]
    df_raw = pd.DataFrame(rows)
    df_raw.to_csv(out_path, index=False, encoding="utf-8-sig")
    print(f"[collect] 원시 데이터 저장: {out_path} ({len(df_raw)}행)")

    # 3. Build and save features
    builder = FeatureBuilder()
    features = builder.build_features(stocks)
    feat_path = builder.save_features(features, date_str=date_str)
    print(f"[collect] 피처 저장: {feat_path} ({len(features)}개)")

    # 4. Print summary table
    if features:
        summary_rows = [
            {
                "종목코드": f.symbol,
                "종목명": f.name,
                "갭률(%)": round(f.gap_rate, 2),
                "시가대비(%)": round(f.open_to_current_rate, 2),
                "규칙점수": round(f.total_rule_score, 2),
            }
            for f in features[:20]
        ]
        df_summary = pd.DataFrame(summary_rows)
        print("\n[collect] 상위 20개 종목 요약:")
        print(df_summary.to_string(index=False))

    print("\n[collect] 완료.")


if __name__ == "__main__":
    main()
