"""Compare new MACD module Strategy B vs old A–F Hist 2-turn on Jul21/Jul22.

Uses the same shared `collect_signed_hist_two_turn_signals` / `evaluate_macd_direction`
and old A–F delay_1m_cons economics. Writes MATCH / STILL_MISMATCH report.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pandas as pd  # noqa: E402

from app.trading.macd_hynix_strategy import (  # noqa: E402
    DIR_DOWN,
    DIR_UP,
    SIGNAL_SYMBOL,
    collect_signed_hist_two_turn_signals,
    evaluate_macd_direction,
    macd_components,
    resample_completed_3m,
    signed_hist_two_turn_pattern,
)
from scripts.replay_macd_hynix_jul21_22 import (  # noqa: E402
    INITIAL_CASH,
    replay_day,
)
from scripts.replay_jul22_macd_williams_strategies import (  # noqa: E402
    compute_metrics,
    execute_signal_strategy,
    _build_indicator_frame,
    signals_B,
)

CACHE = ROOT / "data" / "cache"
STATE = ROOT / "data" / "state"

# Slope-only (pre-fix) for wiggle removal listing
def _slope_only_pattern(h1: float, h2: float, h3: float) -> str:
    d1 = h1 - h2
    d2 = h2 - h3
    if h1 > h2 > h3 and d1 > 0 and d2 > 0:
        return DIR_UP
    if h1 < h2 < h3 and d1 < 0 and d2 < 0:
        return DIR_DOWN
    return "HOLD"


def _norm_ts(ts: Any) -> str:
    if ts is None:
        return ""
    if isinstance(ts, datetime):
        return ts.strftime("%Y-%m-%d %H:%M:%S")
    text = str(ts).replace("T", " ")
    return text[:19]


def _dir_short(d: str) -> str:
    d = str(d or "").upper()
    if d in (DIR_UP, "UP", "UP_RED"):
        return "UP"
    if d in (DIR_DOWN, "DOWN", "DOWN_BLUE"):
        return "DOWN"
    return d


def _module_signals(day: str) -> list[dict[str, str]]:
    path = CACHE / f"replay_{day.replace('-', '')}_hynix_1m.csv"
    df = pd.read_csv(path)
    df["datetime"] = pd.to_datetime(df["datetime"], errors="coerce")
    df = df.dropna(subset=["datetime"]).sort_values("datetime").reset_index(drop=True)
    all_3m = resample_completed_3m(df, now=df["datetime"].iloc[-1] + timedelta(minutes=3))
    last_dir = None
    last_bar = None
    out = []
    for i in range(len(all_3m)):
        bar_ts = pd.Timestamp(all_3m.iloc[i]["datetime"]).to_pydatetime()
        close_ts = bar_ts + timedelta(minutes=3)
        hist_1m = df[df["datetime"] < close_ts]
        ev = evaluate_macd_direction(
            hist_1m,
            now=close_ts,
            last_signal_direction=last_dir,
            last_signal_bar_ts=last_bar,
        )
        if not ev.get("ok") or not ev.get("new_signal"):
            continue
        last_dir = ev["signal_direction"]
        last_bar = ev.get("bar_ts")
        out.append({
            "time": _norm_ts(close_ts),
            "dir": _dir_short(ev["signal_direction"]),
        })
    return out


def _old_af_b_signals(day: str) -> list[dict[str, str]]:
    # Build frames the same way A–F script does (SESSION_DATE is hardcoded Jul22 —
    # for Jul21 load cache directly and build indicator frame).
    tag = day.replace("-", "")
    hynix = pd.read_csv(CACHE / f"replay_{tag}_hynix_1m.csv")
    hynix["datetime"] = pd.to_datetime(hynix["datetime"], errors="coerce")
    hynix = hynix.dropna(subset=["datetime"]).sort_values("datetime").reset_index(drop=True)
    bars = _build_indicator_frame(hynix)
    evs = signals_B(bars)
    return [{"time": _norm_ts(e.signal_time), "dir": e.direction} for e in evs]


def _old_af_b_trades(day: str, scenario: str = "delay_1m_cons") -> dict[str, Any]:
    tag = day.replace("-", "")
    hynix = pd.read_csv(CACHE / f"replay_{tag}_hynix_1m.csv")
    long_1m = pd.read_csv(CACHE / f"replay_{tag}_long_1m.csv")
    inv_1m = pd.read_csv(CACHE / f"replay_{tag}_inverse_1m.csv")
    for df in (hynix, long_1m, inv_1m):
        df["datetime"] = pd.to_datetime(df["datetime"], errors="coerce")
    hynix = hynix.dropna(subset=["datetime"]).sort_values("datetime").reset_index(drop=True)
    long_1m = long_1m.dropna(subset=["datetime"]).sort_values("datetime").reset_index(drop=True)
    inv_1m = inv_1m.dropna(subset=["datetime"]).sort_values("datetime").reset_index(drop=True)
    bars = _build_indicator_frame(hynix)
    events = signals_B(bars)
    # Temporarily patch FORCE_EXIT / ENTRY_CUTOFF for the day
    import scripts.replay_jul22_macd_williams_strategies as af

    old_force = af.FORCE_EXIT
    old_cutoff = af.ENTRY_CUTOFF
    af.FORCE_EXIT = datetime.strptime(f"{day} 15:15:00", "%Y-%m-%d %H:%M:%S")
    af.ENTRY_CUTOFF = datetime.strptime(f"{day} 14:50:00", "%Y-%m-%d %H:%M:%S")
    try:
        rr = execute_signal_strategy(
            "B: Hist 2-turn", events, hynix, long_1m, inv_1m, scenario
        )
        return compute_metrics(rr, hynix)
    finally:
        af.FORCE_EXIT = old_force
        af.ENTRY_CUTOFF = old_cutoff


def _slope_only_signals(day: str) -> list[dict[str, str]]:
    """Pre-fix slope-only flips (for removed-wiggle listing)."""
    path = CACHE / f"replay_{day.replace('-', '')}_hynix_1m.csv"
    df = pd.read_csv(path)
    df["datetime"] = pd.to_datetime(df["datetime"], errors="coerce")
    df = df.dropna(subset=["datetime"]).sort_values("datetime").reset_index(drop=True)
    all_3m = resample_completed_3m(df, now=df["datetime"].iloc[-1] + timedelta(minutes=3))
    last_dir = None
    out = []
    for i in range(len(all_3m)):
        if i < 26:  # still apply warm-up so diff isolates sign gate
            # Pre-fix fired at len>=26 (index 25). Emulate that.
            pass
        bar_ts = pd.Timestamp(all_3m.iloc[i]["datetime"]).to_pydatetime()
        close_ts = bar_ts + timedelta(minutes=3)
        # Pre-fix warm-up: len(bars) >= 26 → index >= 25
        if i < 25:
            continue
        hist_1m = df[df["datetime"] < close_ts]
        bars = resample_completed_3m(hist_1m, now=close_ts)
        if len(bars) < 26:
            continue
        closes = pd.to_numeric(bars["close"], errors="coerce").dropna()
        comps = macd_components(closes)
        if comps["hist"] is None or len(comps["hist"]) < 3:
            continue
        hist = comps["hist"]
        h1, h2, h3 = float(hist.iloc[-1]), float(hist.iloc[-2]), float(hist.iloc[-3])
        pattern = _slope_only_pattern(h1, h2, h3)
        signed = signed_hist_two_turn_pattern(h1, h2, h3)
        if pattern in (DIR_UP, DIR_DOWN) and last_dir != pattern:
            # Would have armed under old slope-only flip gate
            item = {
                "time": _norm_ts(close_ts),
                "dir": _dir_short(pattern),
                "signed_ok": signed == pattern,
            }
            out.append(item)
            last_dir = pattern
    return out


def _compare_day(day: str) -> dict[str, Any]:
    old_sigs = _old_af_b_signals(day)
    new_sigs = _module_signals(day)
    slope_sigs = _slope_only_signals(day)
    removed_wiggles = [
        s for s in slope_sigs
        if not s.get("signed_ok")
    ]
    # Also list slope signals that pass sign but fail warm-up / state vs new
    old_set = {(s["time"], s["dir"]) for s in old_sigs}
    new_set = {(s["time"], s["dir"]) for s in new_sigs}
    only_old = sorted(old_set - new_set)
    only_new = sorted(new_set - old_set)

    old_m = _old_af_b_trades(day)
    new_dr = replay_day(day, delay_min=1, adverse_pct=0.05, delay_label="delay_1m_cons")
    new_net = new_dr.net_pnl
    old_net = float(old_m.get("net_pnl") or 0.0)
    sig_match = old_sigs == new_sigs
    rt_match = int(old_m.get("round_trips") or 0) == new_dr.round_trips
    pnl_match = abs(old_net - new_net) < 1.0  # allow 1 KRW float noise

    return {
        "day": day,
        "signals_before_slope_only": len(slope_sigs),
        "signals_after_signed": len(new_sigs),
        "removed_wiggle_signals": removed_wiggles,
        "old_af_b_signals": old_sigs,
        "new_module_signals": new_sigs,
        "timeline_diff": {
            "only_old_af": [{"time": t, "dir": d} for t, d in only_old],
            "only_new_module": [{"time": t, "dir": d} for t, d in only_new],
        },
        "old_af_b": {
            "signals": len(old_sigs),
            "round_trips": old_m.get("round_trips"),
            "net_pnl": old_m.get("net_pnl"),
            "return_pct": old_m.get("return_pct"),
            "pf": old_m.get("pf") or old_m.get("pf_raw"),
            "mdd_pct": old_m.get("mdd_pct"),
            "trades": old_m.get("trades"),
        },
        "new_module": {
            "signals": len(new_sigs),
            "round_trips": new_dr.round_trips,
            "net_pnl": new_dr.net_pnl,
            "return_pct": new_dr.ret_pct,
            "pf": new_dr.profit_factor,
            "mdd_pct": new_dr.mdd_pct,
            "trades": [
                {
                    "signal_time": t.signal_time,
                    "direction": _dir_short(t.direction),
                    "symbol": t.symbol,
                    "net_pnl": round(t.net_pnl, 2),
                    "exit_reason": t.exit_reason,
                }
                for t in new_dr.trades
            ],
        },
        "signal_times_match": sig_match,
        "round_trips_match": rt_match,
        "pnl_match": pnl_match,
        "day_verdict": "MATCH" if (sig_match and rt_match and pnl_match) else "STILL_MISMATCH",
    }


def main() -> None:
    days = ["2026-07-21", "2026-07-22"]
    by_day = {}
    print("=" * 72)
    print("MACD Strategy B alignment: old A–F B vs new module")
    print("=" * 72)
    for day in days:
        r = _compare_day(day)
        by_day[day] = r
        print(f"\n## {day} → {r['day_verdict']}")
        print(
            f"  signals before(slope)={r['signals_before_slope_only']} "
            f"after(signed)={r['signals_after_signed']} "
            f"removed_wiggles={len(r['removed_wiggle_signals'])}"
        )
        print(f"  old AF signals: {[s['time'][11:16]+s['dir'][0] for s in r['old_af_b_signals']]}")
        print(f"  new module    : {[s['time'][11:16]+s['dir'][0] for s in r['new_module_signals']]}")
        print(
            f"  old RT={r['old_af_b']['round_trips']} net={r['old_af_b']['net_pnl']} "
            f"| new RT={r['new_module']['round_trips']} net={r['new_module']['net_pnl']}"
        )
        if r["removed_wiggle_signals"]:
            print("  removed wiggles:")
            for w in r["removed_wiggle_signals"]:
                print(f"    {w['time'][11:16]} {w['dir']} (slope-only, fails same-sign)")

    all_match = all(by_day[d]["day_verdict"] == "MATCH" for d in days)
    verdict = "MATCH" if all_match else "STILL_MISMATCH"
    report = {
        "generated_at": datetime.now().isoformat(),
        "verdict": verdict,
        "ready_for_mock": verdict == "MATCH",
        "shared_function": "app.trading.macd_hynix_strategy.signed_hist_two_turn_pattern / collect_signed_hist_two_turn_signals / evaluate_macd_direction",
        "economics_note": (
            "Replay comparison uses old A–F B clocks: fill=strict+1m open, "
            "RT_COST_PCT=0.05%, force=15:15, cutoff=14:50. "
            "Live worker keeps 15:00 flatten."
        ),
        "days": by_day,
    }
    out = STATE / "macd_b_signed_alignment_report.json"
    STATE.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"\nVERDICT: {verdict}")
    print(f"READY_FOR_MOCK: {report['ready_for_mock']}")
    print(f"Wrote {out}")


if __name__ == "__main__":
    main()
