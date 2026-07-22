"""Probe afternoon opposite-episode VWAP conditions on Jul21 cache."""
from __future__ import annotations

import sys
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pandas as pd

from app.trading import early_trend_live_feed as feed
from app.trading.etf_entry_confirmation import (
    compute_etf_vwap,
    is_swing_structure_broken_against,
    resolve_window_directions,
    trade_aligned_window_directions,
)
from app.trading.hynix_symbols import LONG_SYMBOL, SHORT_SYMBOL as INVERSE_SYMBOL, SIGNAL_SYMBOL
from app.trading.hynix_switch_risk_gate import is_new_entry_allowed
from scripts.replay_today_weighted_range import _price_at, _slice_to


def main() -> int:
    cache = ROOT / "data" / "cache"
    hynix = pd.read_csv(cache / "replay_20260721_hynix_1m.csv", parse_dates=["datetime"])
    long_df = pd.read_csv(cache / "replay_20260721_long_1m.csv", parse_dates=["datetime"])
    inv_df = pd.read_csv(cache / "replay_20260721_inverse_1m.csv", parse_dates=["datetime"])

    history: dict = {}
    prev_above_by: dict = {}
    existing = None
    start = max(hynix["datetime"].min(), long_df["datetime"].min(), inv_df["datetime"].min())
    start = start.replace(second=0, microsecond=0) + timedelta(minutes=1)
    end = min(hynix["datetime"].max(), long_df["datetime"].max(), inv_df["datetime"].max())
    end = end.replace(second=0, microsecond=0)
    ts = start
    pm_stats = {
        "mismatch": 0,
        "above_and_5_10": 0,
        "reclaim_and_5_10": 0,
        "broken": 0,
        "broken_no_510": 0,
        "above_510_not_reclaim": 0,
    }
    examples = []
    while ts <= end:
        if not is_new_entry_allowed(ts):
            ts += timedelta(seconds=5)
            continue
        sp = _price_at(hynix, ts)
        lp = _price_at(long_df, ts)
        ip = _price_at(inv_df, ts)
        if sp is None or lp is None or ip is None:
            ts += timedelta(seconds=5)
            continue
        history = feed.record_price_sample(history, SIGNAL_SYMBOL, sp, ts)
        history = feed.record_price_sample(history, LONG_SYMBOL, lp, ts)
        history = feed.record_price_sample(history, INVERSE_SYMBOL, ip, ts)
        live = feed.compute_live_trade_direction(
            history,
            ts,
            signal_symbol=SIGNAL_SYMBOL,
            long_symbol=LONG_SYMBOL,
            inverse_symbol=INVERSE_SYMBOL,
        )
        live_dir = live.get("direction")
        if live_dir not in ("UP", "DOWN"):
            ts += timedelta(seconds=5)
            continue
        desired = LONG_SYMBOL if live_dir == "UP" else INVERSE_SYMBOL
        px = lp if desired == LONG_SYMBOL else ip
        etf_slice = _slice_to(long_df if desired == LONG_SYMBOL else inv_df, ts)
        confirm_dirs = trade_aligned_window_directions(
            resolve_window_directions(feed.compute_live_direction(history, desired, ts)),
            symbol=desired,
        )
        vwap = compute_etf_vwap(etf_slice) if len(etf_slice) >= 3 else None
        above = bool(vwap is not None and px >= float(vwap))
        prev = prev_above_by.get(desired)
        reclaim = bool(above and prev is False and confirm_dirs.get(5) == live_dir and confirm_dirs.get(10) == live_dir)
        aligned = confirm_dirs.get(5) == live_dir and confirm_dirs.get(10) == live_dir
        broken = False
        if existing and existing != live_dir:
            edf = long_df if existing == "UP" else inv_df
            ep = lp if existing == "UP" else ip
            es = _slice_to(edf, ts)
            if ep and len(es) >= 3:
                broken = is_swing_structure_broken_against(es, ep, existing)
        if existing and existing != live_dir and ts.hour >= 12:
            pm_stats["mismatch"] += 1
            if broken:
                pm_stats["broken"] += 1
                if not aligned:
                    pm_stats["broken_no_510"] += 1
                    if len(examples) < 15:
                        examples.append((ts.strftime("%H:%M:%S"), "broken_no_510", live_dir, existing, confirm_dirs.get(5), confirm_dirs.get(10)))
            if above and aligned:
                pm_stats["above_and_5_10"] += 1
            if reclaim:
                pm_stats["reclaim_and_5_10"] += 1
            if above and aligned and not reclaim:
                pm_stats["above_510_not_reclaim"] += 1
                if len(examples) < 15:
                    examples.append((ts.strftime("%H:%M:%S"), "above_not_reclaim", live_dir, existing, prev, above))
        # update episode direction loosely for probe (assume switches freely for tracking)
        if existing != live_dir:
            # keep existing sticky until we "confirm" with either path to mimic sticky episode
            if existing is None:
                existing = live_dir
            elif broken or reclaim or (above and aligned):
                existing = live_dir
        prev_above_by[desired] = above
        ts += timedelta(seconds=5)

    print(pm_stats)
    print("examples", examples)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
