"""collect_historical_data.py — SK하이닉스 ML 학습용 과거 1년치 데이터 수집 CLI.

사용법:
    python scripts/collect_historical_data.py [--lookback-days 365]

data/historical/raw/ 에 각 종목/지수별 parquet 캐시와 collection_meta.json을
남긴다. 실패한 항목이 있어도 계속 진행하며, 마지막에 소스/일수 요약을 출력한다.
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

from app.ml.historical_data_loader import collect_all_historical  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="SK하이닉스 ML 학습용 1년치 데이터 수집")
    parser.add_argument("--lookback-days", type=int, default=365)
    parser.add_argument("--out", type=str, default=None, help="수집 요약 JSON 저장 경로(선택)")
    args = parser.parse_args()

    print(f"과거 {args.lookback_days}일 데이터 수집 시작...")
    data = collect_all_historical(lookback_days=args.lookback_days)

    summary = {}
    for key, node in data.items():
        if isinstance(node, dict) and "source" in node:
            summary[key] = {"source": node.get("source"), "days_or_rows": node.get("days") or (
                len(node["df"]) if node.get("df") is not None else 0
            ), "granularity": node.get("granularity"), "error": node.get("error")}

    print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))
    if args.out:
        Path(args.out).write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        print(f"요약 저장: {args.out}")


if __name__ == "__main__":
    main()
