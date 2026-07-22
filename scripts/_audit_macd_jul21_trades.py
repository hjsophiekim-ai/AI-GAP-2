# -*- coding: utf-8 -*-
"""Read-only Jul21 MACD trade audit enrichment. Writes audit artifacts only."""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pandas as pd  # noqa: E402

from app.trading.macd_hynix_strategy import (  # noqa: E402
    DIR_DOWN,
    DIR_UP,
    evaluate_macd_direction,
    macd_components,
    make_direction_episode_id,
    resample_completed_3m,
    _pattern_direction,
)

CACHE = ROOT / "data" / "cache"
STATE = ROOT / "data" / "state"
DAY = "2026-07-21"
TAG = "20260721"


def _old_b_signals(hynix: pd.DataFrame) -> list[dict]:
    all_3m = resample_completed_3m(hynix, now=hynix["datetime"].iloc[-1] + timedelta(minutes=3))
    bars3 = all_3m.copy()
    closes = pd.to_numeric(bars3["close"], errors="coerce")
    comps = macd_components(closes)
    bars3["hist"] = comps["hist"].values
    bars3["close_time"] = bars3["datetime"] + timedelta(minutes=3)
    old_b: list[dict] = []
    for i in range(2, len(bars3)):
        if i < 26:
            continue
        row = bars3.iloc[i]
        h0 = float(bars3.iloc[i - 2]["hist"])
        h1 = float(bars3.iloc[i - 1]["hist"])
        h2 = float(row["hist"])
        d1, d2 = h1 - h0, h2 - h1
        ct = pd.Timestamp(row["close_time"]).to_pydatetime()
        if h1 > 0 and h2 > 0 and d1 > 0 and d2 > 0:
            prev_ok = False
            if i >= 3:
                hp = float(bars3.iloc[i - 3]["hist"])
                dp = h0 - hp
                prev_ok = h0 > 0 and h1 > 0 and dp > 0 and d1 > 0
            if not prev_ok:
                old_b.append(
                    {
                        "time": ct.isoformat(sep="T"),
                        "dir": "UP",
                        "kind": "HIST_2UP_TURN",
                        "hist": [round(h0, 3), round(h1, 3), round(h2, 3)],
                    }
                )
        if h1 < 0 and h2 < 0 and d1 < 0 and d2 < 0:
            prev_ok = False
            if i >= 3:
                hp = float(bars3.iloc[i - 3]["hist"])
                dp = h0 - hp
                prev_ok = h0 < 0 and h1 < 0 and dp < 0 and d1 < 0
            if not prev_ok:
                old_b.append(
                    {
                        "time": ct.isoformat(sep="T"),
                        "dir": "DOWN",
                        "kind": "HIST_2DN_TURN",
                        "hist": [round(h0, 3), round(h1, 3), round(h2, 3)],
                    }
                )
    return old_b


def main() -> None:
    replay = json.loads((STATE / "macd_hynix_jul21_22_replay.json").read_text(encoding="utf-8"))
    day = replay["scenarios"]["delay_1m_cons"][DAY]
    trades = day["trades"]
    signals = day["signal_list"]

    hynix = pd.read_csv(CACHE / f"replay_{TAG}_hynix_1m.csv")
    hynix["datetime"] = pd.to_datetime(hynix["datetime"], errors="coerce")
    hynix = hynix.dropna(subset=["datetime"]).sort_values("datetime").reset_index(drop=True)

    all_3m = resample_completed_3m(hynix, now=hynix["datetime"].iloc[-1] + timedelta(minutes=3))
    last_dir = None
    last_bar = None
    signal_events: list[dict] = []
    for i in range(len(all_3m)):
        bar_ts = pd.Timestamp(all_3m.iloc[i]["datetime"]).to_pydatetime()
        close_ts = bar_ts + timedelta(minutes=3)
        hist_1m = hynix[hynix["datetime"] < close_ts]
        prev_dir = last_dir
        ev = evaluate_macd_direction(
            hist_1m,
            now=close_ts,
            last_signal_direction=last_dir,
            last_signal_bar_ts=last_bar,
        )
        if not ev.get("ok"):
            continue
        bars = resample_completed_3m(hist_1m, now=close_ts)
        closes = pd.to_numeric(bars["close"], errors="coerce").dropna()
        comps = macd_components(closes)
        hist = comps["hist"]
        if hist is None:
            continue
        hist5 = [round(float(x), 6) for x in hist.iloc[-5:].tolist()] if len(hist) >= 5 else [
            round(float(x), 6) for x in hist.tolist()
        ]
        deltas5 = [round(hist5[j + 1] - hist5[j], 6) for j in range(len(hist5) - 1)] if len(hist5) >= 2 else []
        if not ev.get("new_signal"):
            continue
        new_dir = ev["signal_direction"]
        is_first_flip = (prev_dir is None) or (prev_dir != new_dir)
        is_same_dir_repeat = prev_dir == new_dir
        episode_id = make_direction_episode_id(new_dir, ev.get("bar_ts"))
        h0, h1, h2 = float(hist.iloc[-3]), float(hist.iloc[-2]), float(hist.iloc[-1])
        d1, d2 = h1 - h0, h2 - h1
        if new_dir == DIR_UP:
            old_sign_ok = h1 > 0 and h2 > 0 and d1 > 0 and d2 > 0
            new_slope_ok = h2 > h1 > h0 and d1 > 0 and d2 > 0
        else:
            old_sign_ok = h1 < 0 and h2 < 0 and d1 < 0 and d2 < 0
            new_slope_ok = h2 < h1 < h0 and d1 < 0 and d2 < 0
        signal_events.append(
            {
                "signal_close_ts": close_ts.isoformat(sep="T"),
                "bar_ts": ev.get("bar_ts"),
                "bar_close_ts": ev.get("bar_close_ts"),
                "direction": new_dir,
                "prev_direction_state": prev_dir or "NONE",
                "new_direction_state": new_dir,
                "signal_id": ev.get("signal_id"),
                "direction_episode_id": episode_id,
                "is_true_first_flip": bool(is_first_flip and prev_dir is not None),
                "is_initial_entry": prev_dir is None,
                "is_same_direction_repeat": bool(is_same_dir_repeat),
                "reason": ev.get("reason"),
                "hist_last3": ev.get("hist_last3"),
                "hist_deltas_last2": ev.get("hist_deltas"),
                "hist_last5": hist5,
                "hist_deltas_last4": deltas5,
                "display_direction": ev.get("display_direction"),
                "pattern": _pattern_direction(h2, h1, h0),
                "old_B_sign_ok": old_sign_ok,
                "new_B_slope_ok": new_slope_ok,
            }
        )
        last_dir = new_dir
        last_bar = ev.get("bar_ts")

    old_b_21 = _old_b_signals(hynix)

    h22 = pd.read_csv(CACHE / "replay_20260722_hynix_1m.csv")
    h22["datetime"] = pd.to_datetime(h22["datetime"], errors="coerce")
    h22 = h22.dropna(subset=["datetime"]).sort_values("datetime").reset_index(drop=True)
    old_b_22 = _old_b_signals(h22)

    sig_by_time = {s["signal_close_ts"]: s for s in signal_events}
    enriched: list[dict] = []
    prev_exit_reason = None
    for idx, t in enumerate(trades):
        st = t["signal_time"].replace(" ", "T")
        sev = sig_by_time.get(st)
        if sev is None:
            for k, v in sig_by_time.items():
                if k[:16] == st[:16]:
                    sev = v
                    break
        buckets: list[str] = []
        if sev:
            if sev["is_initial_entry"] or sev["is_true_first_flip"]:
                buckets.append("true_direction_flip")
            if sev["is_same_direction_repeat"]:
                buckets.append("same_direction_reentry")
            if sev.get("old_B_sign_ok") is False:
                buckets.append("false_hist_wiggle")
        hold_min = None
        try:
            et = pd.Timestamp(t["entry_time"])
            xt = pd.Timestamp(t["exit_time"])
            hold_min = (xt - et).total_seconds() / 60.0
        except Exception:
            pass
        if hold_min is not None and hold_min <= 12 and "SWITCH" in str(t["exit_reason"]):
            buckets.append("late_or_fast_opposite_switch")
        if prev_exit_reason and "SL" in str(prev_exit_reason):
            buckets.append("reentry_after_SL")
        row = {
            "rt_index": idx + 1,
            **t,
            "hold_min": hold_min,
            "prev_direction_state": sev["prev_direction_state"] if sev else None,
            "new_direction_state": sev["new_direction_state"] if sev else None,
            "signal_id": sev["signal_id"] if sev else None,
            "direction_episode_id": sev["direction_episode_id"] if sev else None,
            "is_true_first_flip": sev["is_true_first_flip"] if sev else None,
            "is_initial_entry": sev["is_initial_entry"] if sev else None,
            "is_same_direction_repeat": sev["is_same_direction_repeat"] if sev else None,
            "hist_last5": sev["hist_last5"] if sev else None,
            "hist_deltas_last4": sev["hist_deltas_last4"] if sev else None,
            "hist_last3": sev["hist_last3"] if sev else None,
            "hist_deltas_last2": sev["hist_deltas_last2"] if sev else None,
            "old_B_sign_ok": sev.get("old_B_sign_ok") if sev else None,
            "new_B_slope_ok": sev.get("new_B_slope_ok") if sev else None,
            "eval_reason": sev.get("reason") if sev else None,
            "loss_buckets": buckets,
        }
        enriched.append(row)
        prev_exit_reason = t["exit_reason"]

    decomp = {
        "true_macd_direction_flip": {"n": 0, "net": 0.0, "gross": 0.0, "cost": 0.0},
        "false_hist_wiggle": {"n": 0, "net": 0.0, "gross": 0.0, "cost": 0.0},
        "same_direction_reentry": {"n": 0, "net": 0.0, "gross": 0.0, "cost": 0.0},
        "reentry_after_SL": {"n": 0, "net": 0.0, "gross": 0.0, "cost": 0.0},
        "late_fast_opposite_switch": {"n": 0, "net": 0.0, "gross": 0.0, "cost": 0.0},
        "trading_costs_all_trades": {"n": 0, "net": 0.0, "gross": 0.0, "cost": 0.0},
    }
    for r in enriched:
        decomp["trading_costs_all_trades"]["n"] += 1
        decomp["trading_costs_all_trades"]["cost"] += r["cost"]
        decomp["trading_costs_all_trades"]["gross"] += r["gross_pnl"]
        decomp["trading_costs_all_trades"]["net"] += r["net_pnl"]
        if r["is_same_direction_repeat"]:
            key = "same_direction_reentry"
        elif r["old_B_sign_ok"] is False:
            key = "false_hist_wiggle"
        else:
            key = "true_macd_direction_flip"
        decomp[key]["n"] += 1
        decomp[key]["net"] += r["net_pnl"]
        decomp[key]["gross"] += r["gross_pnl"]
        decomp[key]["cost"] += r["cost"]
        if "late_or_fast_opposite_switch" in r["loss_buckets"]:
            decomp["late_fast_opposite_switch"]["n"] += 1
            decomp["late_fast_opposite_switch"]["net"] += r["net_pnl"]
            decomp["late_fast_opposite_switch"]["gross"] += r["gross_pnl"]
            decomp["late_fast_opposite_switch"]["cost"] += r["cost"]

    for _k, v in decomp.items():
        for f in ("net", "gross", "cost"):
            v[f] = round(v[f], 2)

    jul22_new = replay["scenarios"]["delay_1m_cons"].get("2026-07-22", {})
    old_jul22 = json.loads((STATE / "jul22_macd_williams_strategies_replay.json").read_text(encoding="utf-8"))
    old_b_jul22 = old_jul22["results"]["delay_1m_cons"]["B: Hist 2-turn"]

    # wiggle-only net among losers
    wiggle_trades = [r for r in enriched if r["old_B_sign_ok"] is False]
    flip_ok_trades = [r for r in enriched if r["old_B_sign_ok"] is True]

    out = {
        "generated_at": datetime.now().isoformat(),
        "day": DAY,
        "verdict": "IMPLEMENTATION_MISMATCH",
        "count_reconcile": {
            "signals": day["signals"],
            "round_trips": day["round_trips"],
            "note": (
                "17 signals / 16 RT: last signal 15:15 after ENTRY_CUTOFF 14:55 — no entry; "
                "prior position already force-closed at 15:00"
            ),
            "orphan_signal": signals[-1] if signals else None,
        },
        "day_summary": {
            "net_pnl": day["net_pnl"],
            "ret_pct": day["ret_pct"],
            "win_rate_pct": day["win_rate_pct"],
            "mdd_pct": day["mdd_pct"],
            "total_gross": round(sum(t["gross_pnl"] for t in trades), 2),
            "total_cost": round(sum(t["cost"] for t in trades), 2),
        },
        "old_B_jul21_signals": old_b_21,
        "old_B_jul21_signal_count": len(old_b_21),
        "new_B_jul21_signal_count": len(signal_events),
        "old_B_jul22_signals": old_b_22,
        "old_B_jul22_signal_count": len(old_b_22),
        "old_compare_B_jul22_delay_1m_cons": {
            "round_trips": old_b_jul22["round_trips"],
            "net_pnl": old_b_jul22["net_pnl"],
            "return_pct": old_b_jul22["return_pct"],
            "trades": old_b_jul22["trades"],
        },
        "new_module_jul22_delay_1m_cons": {
            "signals": jul22_new.get("signals"),
            "round_trips": jul22_new.get("round_trips"),
            "net_pnl": jul22_new.get("net_pnl"),
            "ret_pct": jul22_new.get("ret_pct"),
        },
        "loss_decomposition": decomp,
        "wiggle_vs_sign_ok": {
            "wiggle_n": len(wiggle_trades),
            "wiggle_net": round(sum(r["net_pnl"] for r in wiggle_trades), 2),
            "sign_ok_n": len(flip_ok_trades),
            "sign_ok_net": round(sum(r["net_pnl"] for r in flip_ok_trades), 2),
        },
        "trades": enriched,
        "signals_enriched": signal_events,
    }
    out_path = STATE / "macd_jul21_trade_audit.json"
    out_path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print("Wrote", out_path)
    print("signals", len(signal_events), "RT", len(enriched), "old_B_jul21", len(old_b_21), "old_B_jul22", len(old_b_22))
    print("decomp", json.dumps(decomp, indent=2))
    print("wiggle_vs_sign_ok", out["wiggle_vs_sign_ok"])
    for r in enriched:
        print(
            f"#{r['rt_index']} {str(r['entry_time'])[11:16]}->{str(r['exit_time'])[11:16]} "
            f"{r['direction']} {r['symbol']} prev={r['prev_direction_state']} "
            f"flip={r['is_true_first_flip']}/{r['is_initial_entry']} oldSign={r['old_B_sign_ok']} "
            f"net={r['net_pnl']:.0f} {r['exit_reason']}"
        )
    print("--- old B Jul21 ---")
    for s in old_b_21:
        print(s)
    print("--- old B Jul22 ---")
    for s in old_b_22:
        print(s)


if __name__ == "__main__":
    main()
