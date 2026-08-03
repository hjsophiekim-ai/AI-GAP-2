"""READ-ONLY bar-level root-cause detail for the 5 09:36-12:00 flags Rule B
(2026-07-27 crossover) misses/mis-times against today's KIS chart."""
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
from app.trading.macd2.models import Direction
from app.trading.macd2.signal_engine import calculate_macd, evaluate_macd_crossover, resample_completed_3m

KST = config.KST
TARGETS = ["09:36", "10:30", "10:42", "11:30", "11:39"]


def main():
    now = datetime.now(KST)
    md = MarketDataService(mode="mock")
    boot = md.bootstrap(now=now)
    diag = md.get_last_bootstrap_diag()
    print("warmup prior_trading_day selected_date:", diag.get("prior_trading_day", {}).get("selected_date"))
    print("warmup prior_trading_day received_count:", diag.get("prior_trading_day", {}).get("received_count"))
    print("bootstrap: prior_day_1m_bars=", boot.prior_day_1m_bars, " today_1m_bars=", boot.today_1m_bars,
          " completed_3m_count=", boot.completed_3m_count)

    df_1m = md.get_history_df()
    today_ymd = now.strftime("%Y%m%d")
    bars_3m = resample_completed_3m(df_1m, now=now)
    bars_3m, dropped = filter_complete_3m_bars(bars_3m, df_1m)
    print("dropped (incomplete) 3m bar starts:", dropped)
    today_idx = list(bars_3m.index[bars_3m["datetime"].dt.strftime("%Y%m%d") == today_ymd])
    print(f"today 3m bar count = {len(today_idx)}, total 3m bars (incl warmup) = {len(bars_3m)}")

    # crossover series for direction reference
    last_direction = None
    crossings = {}
    for pos, idx in enumerate(today_idx):
        snap = calculate_macd(bars_3m.iloc[: idx + 1])
        if snap is None:
            continue
        d = evaluate_macd_crossover(snap, last_direction)
        if d != Direction.HOLD:
            last_direction = d
        crossings[idx] = (snap, d)

    for t in TARGETS:
        h, m = int(t.split(":")[0]), int(t.split(":")[1])
        target_dt = now.replace(hour=h, minute=m, second=0, microsecond=0)
        bar_start = target_dt - timedelta(minutes=target_dt.minute % 3)
        print("\n" + "=" * 70)
        print(f"KIS flag time = {t}  ->  containing 3m bar start = {bar_start.strftime('%H:%M')}")
        idx_match = bars_3m.index[bars_3m["datetime"] == bar_start]
        if len(idx_match) == 0:
            print("  !! no matching 3m bar found in reconstructed series (gap?)")
            continue
        idx = int(idx_match[0])

        one_min_rows = df_1m[(df_1m["datetime"] >= bar_start) & (df_1m["datetime"] < bar_start + timedelta(minutes=3))]
        print("  1m OHLCV rows used to build this 3m bar:")
        for _, row in one_min_rows.iterrows():
            print(f"    {row['datetime']}  O={row['open']} H={row['high']} L={row['low']} C={row['close']} V={row['volume']}")
        row3 = bars_3m.iloc[idx]
        print(f"  3m bar (resampled): O={row3['open']} H={row3['high']} L={row3['low']} C={row3['close']} V={row3.get('volume')}")

        for offset in (-2, -1, 0, 1, 2):
            j = idx + offset
            if j < 0 or j >= len(bars_3m):
                continue
            snap = calculate_macd(bars_3m.iloc[: j + 1])
            if snap is None:
                print(f"    bar[{offset:+d}] {bars_3m['datetime'].iloc[j].strftime('%H:%M')}: insufficient bars for MACD yet")
                continue
            crossing = crossings.get(j)
            fired = crossing[1].value if crossing and crossing[1] != Direction.HOLD else "-"
            print(f"    bar[{offset:+d}] {bars_3m['datetime'].iloc[j].strftime('%H:%M')}: "
                  f"macd={snap.macd:.2f} signal={snap.signal:.2f} diff={snap.current_diff:.2f} "
                  f"prev_diff={snap.previous_diff:.2f} hist={snap.hist:.2f} rule_B_fired={fired}")


if __name__ == "__main__":
    main()
