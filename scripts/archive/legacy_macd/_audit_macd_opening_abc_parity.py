"""Read-only audit: A vs C trade parity for MACD opening ABC replay."""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

COMPARE_JSON = ROOT / "data" / "state" / "macd_opening_abc_20d_compare.json"
OUT_JSON = ROOT / "data" / "state" / "macd_opening_abc_parity_audit_detail.json"


def trade_key(t: dict) -> tuple:
    return (t["day"], t["signal_time"], t["direction"], t["symbol"])


def diff_from_compare(data: dict) -> dict:
    a_trades = data["A"]["trades"]
    c_trades = data["C"]["trades"]
    a_keys = {trade_key(t): t for t in a_trades}
    c_keys = {trade_key(t): t for t in c_trades}
    only_a = [a_keys[k] for k in a_keys if k not in c_keys]
    only_c = [c_keys[k] for k in c_keys if k not in a_keys]

    per_day = []
    for day in data["days"]:
        at = [t for t in a_trades if t["day"] == day]
        ct = [t for t in c_trades if t["day"] == day]
        ak = {trade_key(t) for t in at}
        ck = {trade_key(t) for t in ct}
        per_day.append({
            "day": day,
            "a_rt": len(at),
            "c_rt": len(ct),
            "delta_rt": len(at) - len(ct),
            "a_net": round(sum(t["net_pnl"] for t in at), 2),
            "c_net": round(sum(t["net_pnl"] for t in ct), 2),
            "net_delta": round(sum(t["net_pnl"] for t in at) - sum(t["net_pnl"] for t in ct), 2),
            "only_a": [t for t in at if trade_key(t) not in ck],
            "only_c": [t for t in ct if trade_key(t) not in ak],
            "matched": [t for t in at if trade_key(t) in ck],
        })

    net_a_only = sum(t["net_pnl"] for t in only_a)
    net_c_only = sum(t["net_pnl"] for t in only_c)
    return {
        "a_count": len(a_trades),
        "c_count": len(c_trades),
        "only_a": only_a,
        "only_c": only_c,
        "net_a_only": net_a_only,
        "net_c_only": net_c_only,
        "explained_delta": net_a_only - net_c_only,
        "total_net_delta": data["A"]["net"] - data["C"]["net"],
        "per_day": per_day,
    }


def probe_diagnostics(day: str) -> dict:
    """For one day, check if probe would fire and list 09:03 new_signal events."""
    from scripts.compare_macd_opening_abc_20d import (
        _simulate_open_probe_at_900,
        _warmup_hist,
        _session_slice,
        replay_day,
        STRATEGIES,
    )
    from scripts.compare_macd_vs_williams_early_20d import build_day_universe
    from app.trading.macd_hynix_strategy import (
        SIGNAL_SYMBOL,
        evaluate_macd_direction,
        resample_completed_3m,
        tail_prior_day_1m,
    )
    import pandas as pd
    from datetime import timedelta

    dates, _, day_data = build_day_universe(20, refetch_naver=False)
    if day not in dates:
        return {"error": f"{day} not in universe"}

    warm = _warmup_hist(day_data, day, dates)
    hynix_df = day_data[day][SIGNAL_SYMBOL]
    long_df = day_data[day]["0193T0"]
    inv_df = day_data[day]["0197X0"]
    probe_hit = _simulate_open_probe_at_900(warm, hynix_df, day, long_df, inv_df)

    bars3 = resample_completed_3m(
        hynix_df, now=datetime.strptime(f"{day} 15:30:00", "%Y-%m-%d %H:%M:%S")
    )
    idx = dates.index(day)
    warmup_1m = pd.DataFrame()
    if idx > 0:
        prev_day = dates[idx - 1]
        prev = day_data.get(prev_day, {}).get(SIGNAL_SYMBOL)
        warmup_1m = tail_prior_day_1m(_session_slice(prev, prev_day) if prev is not None else pd.DataFrame())

    signals_903 = []
    last_dir = None
    last_bar = None
    for i in range(len(bars3)):
        bar_start = pd.Timestamp(bars3.iloc[i]["datetime"]).to_pydatetime()
        close_ts = bar_start + timedelta(minutes=3)
        today_1m = hynix_df[hynix_df["datetime"] <= close_ts]
        if not warmup_1m.empty:
            sub_1m = pd.concat([warmup_1m, today_1m], ignore_index=True).drop_duplicates("datetime").sort_values("datetime")
        else:
            sub_1m = today_1m
        ev = evaluate_macd_direction(
            sub_1m, now=close_ts, last_signal_direction=last_dir, last_signal_bar_ts=last_bar
        )
        if ev.get("new_signal"):
            last_dir = ev["signal_direction"]
            last_bar = ev.get("bar_ts")
        if close_ts.hour == 9 and close_ts.minute == 3:
            signals_903.append({
                "close_ts": close_ts.isoformat(),
                "new_signal": ev.get("new_signal"),
                "signal_direction": ev.get("signal_direction"),
                "display_direction": ev.get("display_direction"),
            })

    ds_a = replay_day(STRATEGIES[0], day, day_data, dates)
    ds_c = replay_day(STRATEGIES[2], day, day_data, dates)
    return {
        "day": day,
        "warmup_ok": warm.get("ok"),
        "probe_would_fire": probe_hit is not None,
        "probe_detail": probe_hit,
        "open_probe_fired_c": ds_c.open_probe_fired,
        "signals_at_903": signals_903,
        "a_trades": len(ds_a.trades),
        "c_trades": len(ds_c.trades),
        "a_trade_signals": [t.signal_time for t in ds_a.trades],
        "c_trade_signals": [t.signal_time for t in ds_c.trades],
    }


def main() -> int:
    data = json.loads(COMPARE_JSON.read_text(encoding="utf-8"))
    diff = diff_from_compare(data)

    print("=== A vs C trade diff ===")
    print(f"A: {diff['a_count']} RT, C: {diff['c_count']} RT")
    print(f"Only A: {len(diff['only_a'])}, Only C: {len(diff['only_c'])}")
    print(f"Net delta explained: {diff['explained_delta']:,.2f} (total {diff['total_net_delta']:,.2f})")
    print()

    for day_row in diff["per_day"]:
        if day_row["delta_rt"] or day_row["net_delta"]:
            print(f"{day_row['day']}: RT Δ={day_row['delta_rt']} Net Δ={day_row['net_delta']:,.0f}")

    print("\n=== ONLY IN A ===")
    for t in diff["only_a"]:
        print(
            f"  {t['day']} sig={t['signal_time']} {t['direction']} "
            f"exit={t['exit_reason']} net={t['net_pnl']:,.0f}"
        )

    print("\n=== ONLY IN C ===")
    for t in diff["only_c"]:
        print(
            f"  {t['day']} sig={t['signal_time']} {t['direction']} "
            f"exit={t['exit_reason']} net={t['net_pnl']:,.0f}"
        )

    # probe + 09:03 diagnostics on diff days
    diff_days = [d["day"] for d in diff["per_day"] if d["delta_rt"] or d["only_a"] or d["only_c"]]
    probe_info = {}
    for day in diff_days:
        print(f"\n--- Probe/903 diagnostics: {day} ---")
        info = probe_diagnostics(day)
        probe_info[day] = info
        print(f"  warmup_ok={info.get('warmup_ok')} probe_would_fire={info.get('probe_would_fire')} "
              f"open_probe_fired_c={info.get('open_probe_fired_c')}")
        for s in info.get("signals_at_903", []):
            print(f"  09:03: new_signal={s['new_signal']} dir={s.get('signal_direction')}")

    detail = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "compare_source": str(COMPARE_JSON),
        **diff,
        "probe_diagnostics": probe_info,
    }
    # strip full matched trades from per_day for size
    for d in detail["per_day"]:
        d.pop("matched", None)

    OUT_JSON.write_text(json.dumps(detail, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"\nWrote {OUT_JSON}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
