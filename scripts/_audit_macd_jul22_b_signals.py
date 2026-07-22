"""Dump side-by-side Jul22 B signals for mismatch audit."""
from __future__ import annotations

import json
import sys
from datetime import timedelta
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts.replay_jul22_macd_williams_strategies import (  # noqa: E402
    _build_indicator_frame,
    signals_B,
)
from app.trading.macd_hynix_strategy import (  # noqa: E402
    DIR_DOWN,
    DIR_UP,
    evaluate_macd_direction,
    resample_completed_3m,
)

STATE = ROOT / "data" / "state"
CACHE = ROOT / "data" / "cache"


def main() -> None:
    hynix = pd.read_csv(CACHE / "replay_20260722_hynix_1m.csv")
    hynix["datetime"] = pd.to_datetime(hynix["datetime"])
    bars = _build_indicator_frame(hynix)
    sig_prior = signals_B(bars)

    hist_by_ct = {}
    for i in range(len(bars)):
        ct = bars.iloc[i]["close_time"]
        ct = ct.to_pydatetime() if hasattr(ct, "to_pydatetime") else ct
        hist_by_ct[ct] = float(bars.iloc[i]["hist"])
    times = sorted(hist_by_ct)

    def last5(ct):
        if ct not in hist_by_ct:
            return None
        ix = times.index(ct)
        return [round(hist_by_ct[t], 2) for t in times[max(0, ix - 4) : ix + 1]]

    ct_to_i = {}
    for j in range(len(bars)):
        ctb = bars.iloc[j]["close_time"]
        ctb = ctb.to_pydatetime() if hasattr(ctb, "to_pydatetime") else ctb
        ct_to_i[ctb] = j

    all_3m = resample_completed_3m(
        hynix, now=hynix["datetime"].iloc[-1] + timedelta(minutes=3)
    )
    last_dir = None
    last_bar = None
    new_rows = []
    for i in range(len(all_3m)):
        bar_ts = pd.Timestamp(all_3m.iloc[i]["datetime"]).to_pydatetime()
        close_ts = bar_ts + timedelta(minutes=3)
        hist_1m = hynix[hynix["datetime"] < close_ts]
        prev = last_dir
        ev = evaluate_macd_direction(
            hist_1m,
            now=close_ts,
            last_signal_direction=last_dir,
            last_signal_bar_ts=last_bar,
        )
        if not ev.get("ok") or not ev.get("new_signal"):
            continue
        h3 = ev["hist_last3"]
        i_bar = ct_to_i.get(close_ts)
        prior_times = {e.signal_time for e in sig_prior}
        if i_bar is not None and i_bar < 26:
            cls = "WARMUP_PRIOR_SKIPS_i_lt_26"
        elif ev["signal_direction"] == DIR_UP and not (h3[1] > 0 and h3[2] > 0):
            cls = "NO_SIGN_GATE"
        elif ev["signal_direction"] == DIR_DOWN and not (h3[1] < 0 and h3[2] < 0):
            cls = "NO_SIGN_GATE"
        elif close_ts in prior_times:
            cls = "MATCH_PRIOR_ONSET"
        else:
            cls = "SAME_COLOR_BUT_NOT_PRIOR_ONSET"
        new_rows.append(
            {
                "time": close_ts.isoformat(sep=" "),
                "prev_dir": prev,
                "new_dir": ev["signal_direction"],
                "new_entry": True,
                "hist_last5": last5(close_ts),
                "hist_last3": h3,
                "class": cls,
                "reason": ev["reason"],
            }
        )
        last_dir = ev["signal_direction"]
        last_bar = ev.get("bar_ts")

    prior_rows = [
        {
            "time": e.signal_time.isoformat(sep=" "),
            "dir": e.direction,
            "kind": e.kind,
            "hist_last5": last5(e.signal_time),
            "executor_note": "entry only if flat or opposite; same-dir ignored while held",
        }
        for e in sig_prior
    ]

    prior_json = json.loads(
        (STATE / "jul22_macd_williams_strategies_replay.json").read_text(encoding="utf-8")
    )
    new_json = json.loads(
        (STATE / "macd_hynix_jul21_22_replay.json").read_text(encoding="utf-8")
    )
    prior_trades = prior_json["results"]["delay_1m_cons"]["B: Hist 2-turn"]["trades"]
    new_trades = new_json["scenarios"]["delay_1m_cons"]["2026-07-22"]["trades"]
    new_exit = {t["signal_time"].replace("T", " "): t for t in new_trades}
    for r in new_rows:
        t = new_exit.get(r["time"])
        if t:
            r["trade_exit_reason"] = t["exit_reason"]
            r["trade_net"] = t["net_pnl"]
            r["opens_trade"] = True
        else:
            r["opens_trade"] = False

    # Extra RT bucket counts among new trades
    buckets = {
        "NO_SIGN_GATE": 0,
        "WARMUP_PRIOR_SKIPS_i_lt_26": 0,
        "MATCH_PRIOR_ONSET": 0,
        "SAME_COLOR_BUT_NOT_PRIOR_ONSET": 0,
        "OTHER": 0,
    }
    for r in new_rows:
        buckets[r["class"]] = buckets.get(r["class"], 0) + 1

    out = {
        "prior_signals": prior_rows,
        "prior_trades_delay_1m_cons": prior_trades,
        "new_signals": new_rows,
        "new_class_counts": buckets,
        "note": (
            "Prior emits 10 onset signals but only 2 RTs because same-dir repeats "
            "are ignored while held. New emits 14 first-turn flips and takes all 14 RTs."
        ),
    }
    path = STATE / "macd_jul22_b_mismatch_signals.json"
    path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Wrote {path}")
    print(json.dumps(buckets, indent=2))
    for r in new_rows:
        print(
            f"{r['time']} {r['new_dir']} class={r['class']} prev={r['prev_dir']} "
            f"hist5={r['hist_last5']} exit={r.get('trade_exit_reason')}"
        )


if __name__ == "__main__":
    main()
