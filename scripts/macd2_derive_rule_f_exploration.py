"""READ-ONLY exploration script (not one of the two official verification
scripts) -- dumps MACD/Signal/Histogram and their slopes around every GT
flag bar (target bar +/- 2) for 2026-08-03 and 2026-07-31, to look for a
single generalized mathematical onset condition (candidate rule F) that
explains all confirmed KIS flags without date/time hardcoding."""
from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.trading.macd2 import config
from app.trading.macd2.market_data import MarketDataService, filter_complete_3m_bars
from app.trading.macd2.signal_engine import calculate_macd, resample_completed_3m

KST = config.KST
FIXTURE_PATH = ROOT / "data" / "validation" / "macd2" / "kis_expected_flags.csv"


def load_fixture():
    df = pd.read_csv(FIXTURE_PATH, dtype=str)
    df = df[df["confirmed_by_user"].str.lower() == "true"]
    by_date = {}
    for _, row in df.iterrows():
        by_date.setdefault(row["trading_date"], []).append((row["flag_time"], row["direction"]))
    return by_date


def get_bars(day_ymd: str):
    today_str = datetime.now(KST).strftime("%Y%m%d")
    if day_ymd == today_str:
        md = MarketDataService(mode="mock")
        now = datetime.now(KST)
        md.bootstrap(now=now)
        df_1m = md.get_history_df()
    else:
        prior = datetime.strptime(day_ymd, "%Y%m%d").date() - timedelta(days=1)
        while prior.weekday() >= 5:
            prior -= timedelta(days=1)
        frames = []
        for ymd in (prior.strftime("%Y%m%d"), day_ymd):
            path = ROOT / "data" / "cache" / f"replay_{ymd}_hynix_1m.csv"
            frame = pd.read_csv(path)
            frame["datetime"] = pd.to_datetime(frame["datetime"], errors="raise").dt.tz_localize(KST)
            frames.append(frame)
        df_1m = pd.concat(frames, ignore_index=True).sort_values("datetime").reset_index(drop=True)
        now = datetime.strptime(day_ymd, "%Y%m%d").replace(hour=15, minute=30, tzinfo=KST)
    bars_3m = resample_completed_3m(df_1m, now=now)
    bars_3m, _ = filter_complete_3m_bars(bars_3m, df_1m)
    return bars_3m, now


def main():
    fixture = load_fixture()
    for day_ymd, gt in fixture.items():
        bars_3m, now = get_bars(day_ymd)
        print(f"\n{'=' * 80}\nDAY {day_ymd}\n{'=' * 80}")
        for gt_t, gt_d in gt:
            h, m = int(gt_t.split(":")[0]), int(gt_t.split(":")[1])
            bar_start = now.replace(hour=h, minute=m, second=0, microsecond=0) - timedelta(minutes=m % 3)
            idx_match = bars_3m.index[bars_3m["datetime"] == bar_start]
            if len(idx_match) == 0:
                print(f"GT {gt_t} {gt_d}: no bar found")
                continue
            idx = int(idx_match[0])
            print(f"\n--- GT {gt_t} {gt_d} (bar_start={bar_start.strftime('%H:%M')}) ---")
            snaps = {}
            for off in (-3, -2, -1, 0, 1, 2):
                j = idx + off
                if j < 1 or j >= len(bars_3m):
                    continue
                snap = calculate_macd(bars_3m.iloc[: j + 1])
                if snap is None:
                    continue
                snaps[off] = snap
            for off in sorted(snaps):
                s = snaps[off]
                macd_slope = (s.macd - snaps[off - 1].macd) if (off - 1) in snaps else float("nan")
                signal_slope = (s.signal - snaps[off - 1].signal) if (off - 1) in snaps else float("nan")
                hist_slope = (s.hist - snaps[off - 1].hist) if (off - 1) in snaps else float("nan")
                print(f"  [{off:+d}] {s.bar_dt.strftime('%H:%M')}  macd={s.macd:9.2f} signal={s.signal:9.2f} "
                      f"hist={s.hist:9.2f}  macd_slope={macd_slope:9.2f} signal_slope={signal_slope:9.2f} "
                      f"hist_slope={hist_slope:9.2f}")


if __name__ == "__main__":
    main()
