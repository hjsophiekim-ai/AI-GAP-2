"""Read-only audit: Jul22 prior Hist-2-turn B vs macd_hynix_strategy B."""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts.replay_jul22_macd_williams_strategies import (  # noqa: E402
    _build_indicator_frame,
    resample_3m,
    signals_B,
)
from app.trading.macd_hynix_strategy import (  # noqa: E402
    DIR_HOLD,
    _pattern_direction,
    evaluate_macd_direction,
    macd_components,
    resample_completed_3m,
)

CACHE = ROOT / "data" / "cache"
STATE = ROOT / "data" / "state"


def main() -> None:
    hynix = pd.read_csv(CACHE / "replay_20260722_hynix_1m.csv")
    hynix["datetime"] = pd.to_datetime(hynix["datetime"])

    bars_prior = _build_indicator_frame(hynix)
    sig_prior = signals_B(bars_prior)

    all_3m = resample_completed_3m(
        hynix, now=hynix["datetime"].iloc[-1] + timedelta(minutes=3)
    )
    last_dir = None
    last_bar = None
    sig_new = []
    pattern_timeline = []
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
        pattern = ev["display_direction"]
        entry = {
            "close_time": close_ts.isoformat(sep=" "),
            "bar_ts": ev.get("bar_ts"),
            "pattern": pattern,
            "prev_last_dir": prev_dir,
            "new_signal": bool(ev.get("new_signal")),
            "signal_direction": ev.get("signal_direction"),
            "reason": ev.get("reason"),
            "hist_last3": ev.get("hist_last3"),
            "hist_deltas": ev.get("hist_deltas"),
        }
        pattern_timeline.append(entry)
        if ev.get("new_signal"):
            last_dir = ev["signal_direction"]
            last_bar = ev.get("bar_ts")
            sig_new.append(entry)

    bars_new = resample_completed_3m(
        hynix, now=hynix["datetime"].iloc[-1] + timedelta(minutes=3)
    )
    bars_prior_raw = resample_3m(hynix)
    idx = hynix.set_index("datetime")
    counts = idx["close"].resample("3min").count()
    incomplete = counts[counts < 3]

    closes_n = pd.to_numeric(bars_new["close"], errors="coerce")
    comps = macd_components(closes_n)
    bars_new = bars_new.copy()
    bars_new["hist"] = comps["hist"].values

    compare_rows = []
    for i in range(2, len(bars_prior)):
        if i < 26:
            continue
        row = bars_prior.iloc[i]
        h0 = float(bars_prior.iloc[i - 2]["hist"])
        h1 = float(bars_prior.iloc[i - 1]["hist"])
        h2 = float(row["hist"])
        d1, d2 = h1 - h0, h2 - h1
        ct = row["close_time"]
        ct = ct.to_pydatetime() if hasattr(ct, "to_pydatetime") else ct
        prior_up = h1 > 0 and h2 > 0 and d1 > 0 and d2 > 0
        prior_dn = h1 < 0 and h2 < 0 and d1 < 0 and d2 < 0
        prev_ok_up = prev_ok_dn = False
        if i >= 3:
            hp = float(bars_prior.iloc[i - 3]["hist"])
            dp = h0 - hp
            prev_ok_up = h0 > 0 and h1 > 0 and dp > 0 and d1 > 0
            prev_ok_dn = h0 < 0 and h1 < 0 and dp < 0 and d1 < 0
        prior_fire = None
        if prior_up and not prev_ok_up:
            prior_fire = "UP"
        if prior_dn and not prev_ok_dn:
            prior_fire = "DOWN" if prior_fire is None else f"{prior_fire}+DOWN"
        new_pat = _pattern_direction(h2, h1, h0)
        ts = pd.Timestamp(row["datetime"])
        new_row = bars_new[bars_new["datetime"] == ts]
        hist_new = float(new_row.iloc[0]["hist"]) if not new_row.empty else None
        hist_diff = (hist_new - h2) if hist_new is not None else None
        bucket = "none"
        if prior_fire and new_pat == DIR_HOLD:
            bucket = "prior_only"
        elif prior_fire is None and new_pat != DIR_HOLD:
            # why new pattern without prior
            same_sign_pos = h1 > 0 and h2 > 0
            same_sign_neg = h1 < 0 and h2 < 0
            mono_up = h2 > h1 > h0 and d1 > 0 and d2 > 0
            mono_dn = h2 < h1 < h0 and d1 < 0 and d2 < 0
            if mono_up or mono_dn:
                if not (same_sign_pos or same_sign_neg):
                    bucket = "new_only_missing_sign_gate"
                elif (prior_up and prev_ok_up) or (prior_dn and prev_ok_dn):
                    bucket = "new_only_continuation_not_onset"
                else:
                    bucket = "new_only_other_rule"
            else:
                bucket = "new_only_unexpected"
        elif prior_fire:
            bucket = "both_pattern_onset"
        compare_rows.append(
            {
                "close_time": ct.isoformat(sep=" "),
                "hist3_prior": [round(h0, 4), round(h1, 4), round(h2, 4)],
                "signs": [
                    "pos" if h0 > 0 else "neg",
                    "pos" if h1 > 0 else "neg",
                    "pos" if h2 > 0 else "neg",
                ],
                "deltas": [round(d1, 4), round(d2, 4)],
                "prior_fire": prior_fire,
                "new_pattern": new_pat,
                "hist_prior": round(h2, 6),
                "hist_new": round(hist_new, 6) if hist_new is not None else None,
                "hist_diff": round(hist_diff, 9) if hist_diff is not None else None,
                "bucket": bucket,
            }
        )

    # Classify each NEW signal vs prior
    prior_times = {e.signal_time.isoformat(sep=" "): e for e in sig_prior}
    classifications = []
    for s in sig_new:
        ct = s["close_time"]
        h3 = s["hist_last3"]  # [oldest, mid, newest]
        signs = ["pos" if x > 0 else "neg" for x in h3]
        same_sign = signs[1] == signs[2] and (
            (h3[1] > 0 and h3[2] > 0) or (h3[1] < 0 and h3[2] < 0)
        )
        in_prior = ct in prior_times
        if in_prior:
            cls = "matches_prior_B"
        elif not same_sign:
            cls = "missing_prior_sign_gate"  # fires on mono hist without same color
        else:
            cls = "same_sign_but_not_prior_onset"
        classifications.append(
            {
                "close_time": ct,
                "direction": s["signal_direction"],
                "prev_last_dir": s["prev_last_dir"],
                "hist_last3": h3,
                "signs": signs,
                "same_sign_last2": same_sign,
                "in_prior_signals": in_prior,
                "class": cls,
                "reason": s["reason"],
            }
        )

    # Load existing trade artifacts for side-by-side
    prior_json = json.loads(
        (STATE / "jul22_macd_williams_strategies_replay.json").read_text(encoding="utf-8")
    )
    new_json = json.loads((STATE / "macd_hynix_jul21_22_replay.json").read_text(encoding="utf-8"))
    prior_b = prior_json["results"]["delay_1m_cons"]["B: Hist 2-turn"]
    new_b = new_json["scenarios"]["delay_1m_cons"]["2026-07-22"]

    # Data paths shared
    data_parity = {}
    for sym, name in [
        ("000660", "replay_20260722_hynix_1m.csv"),
        ("0193T0", "replay_20260722_long_1m.csv"),
        ("0197X0", "replay_20260722_inverse_1m.csv"),
    ]:
        p = CACHE / name
        df = pd.read_csv(p)
        df["datetime"] = pd.to_datetime(df["datetime"])
        data_parity[sym] = {
            "path": str(p),
            "rows": len(df),
            "min": str(df["datetime"].min()),
            "max": str(df["datetime"].max()),
            "sample": {
                t: {
                    "open": float(r.open),
                    "high": float(r.high),
                    "low": float(r.low),
                    "close": float(r.close),
                }
                for t, r in [
                    (
                        t,
                        df[df["datetime"] == pd.Timestamp(f"2026-07-22 {t}")].iloc[0],
                    )
                    for t in ["10:42", "12:33", "15:00"]
                    if not df[df["datetime"] == pd.Timestamp(f"2026-07-22 {t}")].empty
                ]
            },
        }

    # Extra trades analysis: new has 14 RT, prior 2 — map new trades
    new_trades = new_b["trades"]
    prior_trade_signals = {t["signal_time"] for t in prior_b["trades"]}
    # normalize prior times
    prior_trade_signals = {t.replace(" ", "T") if "T" not in t else t for t in prior_trade_signals}
    prior_trade_signals |= {t.replace("T", " ") for t in list(prior_trade_signals)}

    extra_trade_buckets = []
    for t in new_trades:
        st = t["signal_time"].replace("T", " ")
        matching_cls = next(
            (c for c in classifications if c["close_time"] == st),
            None,
        )
        # No TP/SL in jul21_22 replay — all switches or force
        if t["exit_reason"].startswith("SWITCH") or t["exit_reason"] in (
            "15:00_FORCE",
            "EOD_FLAT",
        ):
            if matching_cls and matching_cls["class"] == "matches_prior_B":
                bucket = "aligned_with_prior"
            elif matching_cls and matching_cls["class"] == "missing_prior_sign_gate":
                bucket = "extra_due_to_missing_sign_gate"
            elif matching_cls and matching_cls["class"] == "same_sign_but_not_prior_onset":
                bucket = "extra_due_to_onset_vs_first_turn_diff"
            else:
                bucket = "other"
        else:
            bucket = f"exit_{t['exit_reason']}"
        extra_trade_buckets.append(
            {
                "signal_time": st,
                "direction": t["direction"],
                "exit_reason": t["exit_reason"],
                "net_pnl": t["net_pnl"],
                "in_prior_trades": st in prior_trade_signals
                or st.replace(" ", "T") in prior_trade_signals,
                "signal_class": matching_cls["class"] if matching_cls else None,
                "bucket": bucket,
            }
        )

    max_abs_hist_diff = max(
        (abs(r["hist_diff"]) for r in compare_rows if r["hist_diff"] is not None),
        default=0.0,
    )

    report = {
        "verdict": "IMPLEMENTATION_MISMATCH",
        "day": "2026-07-22",
        "headline": {
            "prior_B_delay_1m_cons": {
                "round_trips": prior_b["round_trips"],
                "net_pnl": prior_b["net_pnl"],
                "return_pct": prior_b["return_pct"],
                "signals": [
                    {"time": e.signal_time.isoformat(sep=" "), "dir": e.direction, "kind": e.kind}
                    for e in sig_prior
                ],
            },
            "new_macd_hynix_delay_1m_cons": {
                "round_trips": new_b["round_trips"],
                "net_pnl": new_b["net_pnl"],
                "return_pct": new_b["ret_pct"],
                "signals": len(sig_new),
            },
        },
        "data_parity": {
            "both_use_same_cache_files": True,
            "files": data_parity,
            "resample": {
                "prior_resample_3m_full3_required": len(bars_prior_raw),
                "prior_build_indicator_frame": len(bars_prior),
                "new_resample_completed_3m": len(bars_new),
                "incomplete_3m_buckets_count_lt_3": int(len(incomplete)),
                "incomplete_buckets": [
                    {"ts": str(ts), "count": int(c)} for ts, c in incomplete.items()
                ],
                "max_abs_hist_diff_aligned_bars": max_abs_hist_diff,
            },
        },
        "definition_diff": {
            "prior_signals_B": (
                "Requires last TWO hist same sign (color) AND two consecutive "
                "same-sign deltas; fires only on pattern ONSET (prev bar not already qualifying)."
            ),
            "new_evaluate_macd_direction": (
                "Requires last THREE hist strictly monotonic (2 consecutive deltas same sign); "
                "NO hist sign/color gate. Fires on FIRST TURN into UP_RED/DOWN_BLUE vs "
                "last_signal_direction (persists across day; not reset on flat/TP/SL)."
            ),
        },
        "prior_signals": [
            {"time": e.signal_time.isoformat(sep=" "), "dir": e.direction, "kind": e.kind}
            for e in sig_prior
        ],
        "new_signals": classifications,
        "new_signal_class_counts": {
            k: sum(1 for c in classifications if c["class"] == k)
            for k in sorted({c["class"] for c in classifications})
        },
        "extra_trades": extra_trade_buckets,
        "extra_trade_bucket_counts": {
            k: sum(1 for c in extra_trade_buckets if c["bucket"] == k)
            for k in sorted({c["bucket"] for c in extra_trade_buckets})
        },
        "pattern_bars_of_interest": [
            r for r in compare_rows if r["bucket"] != "none" or r["prior_fire"] or r["new_pattern"] != DIR_HOLD
        ],
        "new_pattern_timeline_new_signals_only": sig_new,
        "fill_cost_diff_notes": {
            "prior": "resolve_fill: delay_1m_cons = next 1m open STRICTLY after signal minute + 0.05% adverse; RT_COST_PCT=0.05% of entry notional",
            "new": "_fill_price: delay_1m = first 1m bar with datetime >= signal_close_ts open + 0.05% adverse; TradeCostEngine market costs; avg_signal_to_fill_sec observed 0.0 (fills at signal close minute open, not +1m)",
            "force_exit": "prior FORCE 15:15; new FORCE 15:00",
            "entry_cutoff": "prior 14:50; new 14:55",
            "TP_SL": "neither Jul22 B replay uses TP/SL exits in these artifacts (new module has TP/SL helpers but jul21_22 replay switches on every opposite first-turn)",
        },
    }

    out_json = STATE / "macd_jul22_b_mismatch_audit.json"
    out_json.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"wrote": str(out_json), "summary": {
        "prior_signals": len(sig_prior),
        "new_signals": len(sig_new),
        "new_signal_class_counts": report["new_signal_class_counts"],
        "extra_trade_bucket_counts": report["extra_trade_bucket_counts"],
        "incomplete_3m": int(len(incomplete)),
        "max_hist_diff": max_abs_hist_diff,
        "prior_RT_net": [prior_b["round_trips"], prior_b["return_pct"]],
        "new_RT_net": [new_b["round_trips"], new_b["ret_pct"]],
    }}, indent=2))

    print("\n=== PRIOR B ===")
    for e in sig_prior:
        print(f"  {e.signal_time} {e.direction} {e.kind}")
    print("\n=== NEW B ===")
    for c in classifications:
        print(
            f"  {c['close_time']} {c['direction']} class={c['class']} "
            f"prev={c['prev_last_dir']} signs={c['signs']} hist={c['hist_last3']}"
        )


if __name__ == "__main__":
    main()
