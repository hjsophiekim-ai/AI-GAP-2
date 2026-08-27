#!/usr/bin/env python
"""READ-ONLY TRAIN/OOS validation for the FROZEN variant-C decision -- 2026-
08-27 user request. The condition LOGIC in scripts/teg_gate_v2.py is frozen
and untouched (imported, never edited) -- this script only recalibrates the
"non-trivial" thresholds (TEG_V2_HIST_DELTA_FLOOR / TEG_V2_SPREAD_DELTA_
FLOOR) using ONLY the TRAIN period's own bar-to-bar delta distribution
(closing scripts/_tmp_teg_v2_threshold_calibration.py's look-ahead/OOS-
contamination gap, which had calibrated off the FULL 60-day window), then
monkeypatches teg_gate_v2's two module-level constants to those TRAIN-only
values for the duration of this run (same technique scripts/tw_gate_
relaxed_optimization.py's own docstring documents for sweeping *_PCT
constants -- never edits the file on disk). Frozen thresholds are then used
UNCHANGED for both TRAIN and OOS backtests -- never recomputed on OOS.

Variant B is intentionally NOT run here -- user locked in variant C only.

Never touches app/trading/macd2 production code or scripts/teg_gate_v2.py.
"""
from __future__ import annotations

import json
import sys
from dataclasses import asdict
from pathlib import Path

import numpy as np
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
if str(PROJECT_ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

import scripts.tw_gate_corrected_clock_engine as cce  # noqa: E402
import scripts.teg_backtest_60day_v2 as tb2  # noqa: E402
import scripts.teg_gate_v2 as teg2  # noqa: E402
from app.trading.macd2 import time_window_filter as twf  # noqa: E402
from app.trading.macd2.major_flag_filter import _prepare_bars  # noqa: E402
from app.trading.macd2.models import Direction  # noqa: E402

CACHE_DIR = PROJECT_ROOT / "data" / "cache"
OUTPUT_DIR = PROJECT_ROOT / "data" / "validation" / "teg_c_train_oos"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

ALL_DATES = sorted({p.stem.split("_")[1] for p in CACHE_DIR.glob("replay_*_hynix_1m.csv")})[-60:]
TRAIN_DATES = ALL_DATES[:40]
OOS_DATES = ALL_DATES[40:]
assert len(TRAIN_DATES) == 40 and len(OOS_DATES) == 20


def calibrate_thresholds(dates: list[str]) -> tuple[float, float, int]:
    """20th percentile of bar-to-bar |Δhist| / |Δ(EMA10-EMA20)|, computed
    ONLY over the given ``dates`` -- same method as the original (full-
    window) calibration, just scoped to TRAIN only."""
    cache, _ = cce.prepare_cache(dates)
    hist_deltas: list[float] = []
    spread_deltas: list[float] = []
    for day in cache:
        work = _prepare_bars(day["hynix_bars_3m"])
        if work is None or len(work) < 5:
            continue
        series = twf._gap_series(work)
        if series is None:
            continue
        gap = series["gap"].to_numpy()
        hist_deltas.extend(np.abs(np.diff(gap)).tolist())

        close = work["close"].astype(float).reset_index(drop=True)
        ema10 = close.ewm(span=10, adjust=False).mean()
        ema20 = close.ewm(span=20, adjust=False).mean()
        spread = (ema10 - ema20).to_numpy()
        spread_deltas.extend(np.abs(np.diff(spread)).tolist())

    hist_floor = float(np.percentile(np.array(hist_deltas), 20))
    spread_floor = float(np.percentile(np.array(spread_deltas), 20))
    return hist_floor, spread_floor, len(hist_deltas)


def _metrics(trades: list, n_days: int) -> dict:
    closed = [t for t in trades if t.net_return_pct is not None]
    wins = [t for t in closed if t.net_return_pct > 0]
    total = sum(t.net_return_pct for t in closed)
    compounded = 1.0
    equity = peak = max_dd = 0.0
    for t in sorted(closed, key=lambda t: t.entry_time):
        compounded *= (1.0 + t.net_return_pct / 100.0)
        equity += t.net_return_pct
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)
    return {
        "trading_days": n_days, "entries": len(closed), "wins": len(wins),
        "win_rate_pct": round(len(wins) / len(closed) * 100.0, 1) if closed else 0.0,
        "total_pct": round(total, 3), "compound_pct": round((compounded - 1.0) * 100.0, 3),
        "avg_pct_per_trade": round(total / len(closed), 3) if closed else 0.0,
        "mdd_pct": round(max_dd, 3),
    }


def run_period(dates: list[str], label: str) -> dict:
    cache, notes = cce.prepare_cache(dates)
    results = {}
    for variant in ("A", "C"):
        trades, flag_log = tb2.run_over_cache(cache, variant)
        m = _metrics(trades, len(cache))
        results[variant] = m
        (OUTPUT_DIR / f"trades_{label}_{variant}.json").write_text(
            json.dumps([asdict(t) for t in trades], ensure_ascii=False, indent=2, default=str), encoding="utf-8",
        )
        (OUTPUT_DIR / f"flag_log_{label}_{variant}.json").write_text(
            json.dumps(flag_log, ensure_ascii=False, indent=2, default=str), encoding="utf-8",
        )
    return results


def check_target_flag(date: str, flag_iso: str, direction: Direction, label: str) -> dict:
    # cce.prepare_day already loads the prior day's warmup bars internally
    # (base._load_hynix_with_warmup) -- no need to pass an extra date here.
    cache, _ = cce.prepare_cache([date])
    by_date = {d["date"]: d for d in cache}
    day = by_date[date]
    hynix_bars_3m = day["hynix_bars_3m"]
    flag_idx = None
    flag_bar_dt = None
    for fidx, fdir in day["flags"]:
        bt = hynix_bars_3m["datetime"].iloc[fidx].to_pydatetime()
        if bt.isoformat() == flag_iso and fdir == direction:
            flag_idx = fidx
            flag_bar_dt = bt
            break
    if flag_idx is None:
        return {"error": f"flag not found for {date} {flag_iso} {direction}"}
    from datetime import timedelta
    confirm_idx = flag_idx + 1
    decision_at = hynix_bars_3m["datetime"].iloc[confirm_idx].to_pydatetime() + timedelta(minutes=3)
    truncated = hynix_bars_3m.iloc[: confirm_idx + 1]
    d = teg2.evaluate_teg(truncated, direction, flag_bar_dt, decision_at)
    return {
        "label": label, "date": date, "flag": flag_iso, "direction": direction.value,
        "approved": d.approved, "conditions": d.conditions, "metrics": d.metrics,
        "reject_reasons": list(d.reject_reasons),
    }


def main():
    print(f"ALL_DATES: {len(ALL_DATES)} days, {ALL_DATES[0]}..{ALL_DATES[-1]}")
    print(f"TRAIN: {len(TRAIN_DATES)} days, {TRAIN_DATES[0]}..{TRAIN_DATES[-1]}")
    print(f"OOS:   {len(OOS_DATES)} days, {OOS_DATES[0]}..{OOS_DATES[-1]}")

    hist_floor, spread_floor, n = calibrate_thresholds(TRAIN_DATES)
    print(f"\nTRAIN-only calibration (n={n} bar-to-bar deltas, TRAIN dates only):")
    print(f"  TEG_V2_HIST_DELTA_FLOOR   = {hist_floor:.3f}  (full-window v1 value was 142.11)")
    print(f"  TEG_V2_SPREAD_DELTA_FLOOR = {spread_floor:.3f}  (full-window v1 value was 203.37)")

    # Freeze -- monkeypatch teg_gate_v2's module constants (file itself never
    # edited); teg_backtest_60day_v2 imported the SAME module object, so its
    # calls to teg.evaluate_teg pick these up automatically.
    teg2.TEG_V2_HIST_DELTA_FLOOR = hist_floor
    teg2.TEG_V2_SPREAD_DELTA_FLOOR = spread_floor

    print("\n=== TRAIN period (reference only) ===")
    train_results = run_period(TRAIN_DATES, "TRAIN")
    for name, m in train_results.items():
        print(f"  {name}: entries={m['entries']} wins={m['wins']} win%={m['win_rate_pct']} total%={m['total_pct']} compound%={m['compound_pct']} avg/trd%={m['avg_pct_per_trade']} MDD%={m['mdd_pct']}")

    print("\n=== OOS period (decision-relevant) ===")
    oos_results = run_period(OOS_DATES, "OOS")
    for name, m in oos_results.items():
        print(f"  {name}: entries={m['entries']} wins={m['wins']} win%={m['win_rate_pct']} total%={m['total_pct']} compound%={m['compound_pct']} avg/trd%={m['avg_pct_per_trade']} MDD%={m['mdd_pct']}")

    a, c = oos_results["A"], oos_results["C"]
    print("\n=== OOS: does C improve on ALL FOUR vs A? ===")
    verdict = {
        "entries_C_gt_A": c["entries"] > a["entries"],
        "total_pct_C_gt_A": c["total_pct"] > a["total_pct"],
        "compound_pct_C_gt_A": c["compound_pct"] > a["compound_pct"],
        "mdd_pct_C_lt_or_eq_A": c["mdd_pct"] <= a["mdd_pct"],
    }
    for k, v in verdict.items():
        print(f"  {k}: {'YES' if v else 'NO'}")
    print(f"  A: entries={a['entries']} total%={a['total_pct']} compound%={a['compound_pct']} MDD%={a['mdd_pct']}")
    print(f"  C: entries={c['entries']} total%={c['total_pct']} compound%={c['compound_pct']} MDD%={c['mdd_pct']}")

    print("\n=== Target-flag re-check under FROZEN TRAIN-derived thresholds ===")
    check1 = check_target_flag("20260825", "2026-08-25T12:09:00+09:00", Direction.UP_RED, "8/25 12:09 UP_RED")
    check2 = check_target_flag("20260826", "2026-08-26T11:06:00+09:00", Direction.UP_RED, "8/26 11:06 UP_RED")
    for chk in (check1, check2):
        print(f"  {chk['label']}: approved={chk['approved']} reject_reasons={chk.get('reject_reasons')}")

    summary = {
        "all_dates_range": [ALL_DATES[0], ALL_DATES[-1]],
        "train_dates_range": [TRAIN_DATES[0], TRAIN_DATES[-1]],
        "oos_dates_range": [OOS_DATES[0], OOS_DATES[-1]],
        "train_hist_delta_floor": hist_floor, "train_spread_delta_floor": spread_floor,
        "train_results": train_results, "oos_results": oos_results, "oos_verdict": verdict,
        "target_flag_checks": [check1, check2],
    }
    (OUTPUT_DIR / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"\nSaved: {OUTPUT_DIR / 'summary.json'}")


if __name__ == "__main__":
    main()
