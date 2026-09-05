#!/usr/bin/env python
"""READ-ONLY data fetch (2026-09-04): pull today's real 1-minute
hynix/LONG-ETF/INVERSE-ETF bars via KIS quotation-only calls and save them
as the standard replay_YYYYMMDD_*_1m.csv cache files, so the 30-business-day
full-chain backtest can include today.

No production code is modified — this only reuses
scripts/fetch_and_analyze_macd2_today.fetch_today / save_replay_csvs.
"""
from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
if str(PROJECT_ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

import scripts.fetch_and_analyze_macd2_today as fa  # noqa: E402
from app.trading.kis_client import create_kis_client  # noqa: E402

DATE = sys.argv[1] if len(sys.argv) > 1 else "20260904"


def main() -> int:
    client = create_kis_client("real") or create_kis_client("mock")
    if client is None:
        print("KIS client unavailable")
        return 1
    frames = fa.fetch_today(client, DATE)
    for tag, df in frames.items():
        if df is None or df.empty:
            print(f"  {tag}: EMPTY")
        else:
            print(f"  {tag}: {len(df)} bars {df['datetime'].iloc[0]} .. {df['datetime'].iloc[-1]}")
    if frames["hynix"].empty:
        print("NO HYNIX DATA -- not saving")
        return 1
    fa.save_replay_csvs(DATE, frames)
    print(f"saved replay_{DATE}_*_1m.csv")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
