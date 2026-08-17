#!/usr/bin/env python
"""Manual/backfill CLI for the daily 1-minute-bar archive.

The actual fetch/save logic (and the automatic once-a-day-after-16:00 KST
trigger running inside the deployed web service) lives in
app.services.minute_bar_archiver / app.services.minute_bar_archive_scheduler
-- this script is a thin wrapper for running it by hand (e.g. to backfill a
specific older date the automatic LOOKBACK_CALENDAR_DAYS window already
missed, or to check status locally).

Usage:
  python scripts/save_daily_minute_bars.py                  # auto: fill any
    missing trading day in the last LOOKBACK_CALENDAR_DAYS calendar days
  python scripts/save_daily_minute_bars.py 20260817 20260818 # explicit dates
"""
from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.services.minute_bar_archiver import run_archive  # noqa: E402
from app.trading.kis_client import create_kis_client  # noqa: E402

if __name__ == "__main__":
    explicit_dates = sys.argv[1:]

    client = create_kis_client(mode="real")
    if client is None:
        print("KIS real client unavailable (missing KIS_REAL_APP_KEY/SECRET?) -- nothing fetched.")
        sys.exit(1)

    results = run_archive(client, explicit_dates or None, source="manual_cli")
    for r in results:
        print(f"  {r['date']}: {r['status']}  counts={r.get('counts')}")

    saved = [r for r in results if r["status"] == "saved"]
    print(f"\nDone. Newly saved: {len(saved)} date(s): {[r['date'] for r in saved]}")
