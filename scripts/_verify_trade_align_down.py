"""Verify trade_align vs raw slopes at DOWN miss samples."""
from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pandas as pd

from app.trading import early_trend_live_feed as feed
from app.trading.etf_entry_confirmation import (
    compute_etf_vwap,
    resolve_window_directions,
    trade_aligned_window_directions,
)
from app.trading.hynix_symbols import LONG_SYMBOL, SHORT_SYMBOL as INVERSE_SYMBOL, SIGNAL_SYMBOL
from scripts.replay_today_weighted_range import _price_at

h = pd.read_csv(ROOT / "data/cache/replay_20260721_hynix_1m.csv", parse_dates=["datetime"])
l = pd.read_csv(ROOT / "data/cache/replay_20260721_long_1m.csv", parse_dates=["datetime"])
i = pd.read_csv(ROOT / "data/cache/replay_20260721_inverse_1m.csv", parse_dates=["datetime"])

history: dict = {}
start = datetime(2026, 7, 21, 9, 1)
end = datetime(2026, 7, 21, 14, 40)
ts = start
samples = []
while ts <= end:
    sp, lp, ip = _price_at(h, ts), _price_at(l, ts), _price_at(i, ts)
    if None not in (sp, lp, ip):
        history = feed.record_price_sample(history, SIGNAL_SYMBOL, sp, ts)
        history = feed.record_price_sample(history, LONG_SYMBOL, lp, ts)
        history = feed.record_price_sample(history, INVERSE_SYMBOL, ip, ts)
        live = feed.compute_live_trade_direction(
            history, ts, signal_symbol=SIGNAL_SYMBOL, long_symbol=LONG_SYMBOL, inverse_symbol=INVERSE_SYMBOL
        )
        if live.get("direction") == "DOWN" and "13:22" <= ts.strftime("%H:%M") <= "14:17" and ts.second == 0:
            raw = resolve_window_directions(feed.compute_live_direction(history, INVERSE_SYMBOL, ts))
            aligned = trade_aligned_window_directions(raw, symbol=INVERSE_SYMBOL)
            etf_slice = i[i["datetime"] <= ts.replace(second=0, microsecond=0)]
            vwap = compute_etf_vwap(etf_slice) if len(etf_slice) >= 3 else None
            samples.append({
                "t": ts.strftime("%H:%M:%S"),
                "inv_px": round(ip, 2),
                "vwap": round(float(vwap), 2) if vwap else None,
                "above": bool(vwap and ip >= float(vwap)),
                "raw_5_10": f"{raw.get(5)}/{raw.get(10)}",
                "aligned_5_10": f"{aligned.get(5)}/{aligned.get(10)}",
            })
    ts += timedelta(seconds=5)

print("DOWN live :00 samples with raw vs aligned inverse slopes")
for s in samples:
    print(s)
