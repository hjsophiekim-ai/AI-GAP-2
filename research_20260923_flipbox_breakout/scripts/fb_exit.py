"""Match flip-box breakouts against real N1+C1 trades (entry/exit timestamps)."""
import sys, pandas as pd, numpy as np
from datetime import timedelta
sys.path.insert(0, r"C:\Users\FURSYS\Desktop\AI-GAP 2")
import fb_lib as F, fb_run as R
from app.trading.macd2.worker import _net_return_pct

from pathlib import Path as _P
TR = _P(__file__).resolve().parent.parent / "engine_out" / "n1c1_trades.csv"
BUDGET = 10_000_000.0   # per-slot strategy budget (see project_macd2_daily_budget_is_already_30m)


def main():
    U = pd.read_pickle("union_breakouts.pkl")
    T = pd.read_csv(TR)
    T["date"] = T["date"].astype(str)
    for c in ("entry_time", "exit_time", "h50_hold_at", "ax_at"):
        T[c] = pd.to_datetime(T[c], errors="coerce", utc=True).dt.tz_convert(F.KST)
    days, fbd, cov = R.load_all()
    print("trades %d  %s..%s" % (len(T), T.date.min(), T.date.max()))
    rows = []
    for _, r in U.iterrows():
        if r["date"] < T.date.min() or r["date"] > T.date.max():
            continue
        fill = r["fill_at"]
        same = T[(T.date == r["date"]) & (T.entry_time <= fill) & (T.exit_time > fill)]
        opp = same[same.direction != r["last_dir"]]
        for _, t in opp.iterrows():
            db = days[r["date"]]
            tag = "long" if t["direction"] == "UP_RED" else "inverse"
            p = db.px.get(tag)
            px = None if p is None else (p.exact(fill) or p.close_at(fill))
            if px is None or not t["entry_price"]:
                continue
            bo_net = _net_return_pct(t["entry_symbol"], float(t["entry_price"]), float(px), 1000)
            rows.append(dict(
                date=r["date"], bo_bar=r["bo_bar"], bo_dir=r["last_dir"],
                held_dir=t["direction"], entry_time=t["entry_time"], entry_px=t["entry_price"],
                exit_time=t["exit_time"], exit_px=t["exit_price"], exit_reason=t["exit_reason"],
                actual_net=t["net_pct"], bo_net=bo_net, uplift_pct=bo_net - t["net_pct"],
                w1a=t["w1a"], h50_held=t["h50_held"], h50_hold_at=t["h50_hold_at"],
                peak=t["peak_net_pct"], mae=t["mae_net_pct"], hold_min=t["hold_minutes"],
                mins_to_actual_exit=(t["exit_time"] - fill).total_seconds() / 60.0,
            ))
    E = pd.DataFrame(rows)
    if E.empty:
        print("no matches"); return
    E["uplift_krw"] = E.uplift_pct / 100.0 * BUDGET * E.w1a
    E["h50_active"] = E.h50_hold_at.notna() & (E.h50_hold_at <= E.bo_bar)
    E.to_csv("exit_matched.csv", index=False)
    pd.set_option("display.width", 300)
    show = E.copy()
    for c in ("bo_bar", "entry_time", "exit_time"):
        show[c] = show[c].dt.strftime("%m-%d %H:%M")
    print(show[["date", "bo_bar", "bo_dir", "held_dir", "entry_time", "exit_time", "exit_reason",
                "actual_net", "bo_net", "uplift_pct", "uplift_krw", "h50_held", "h50_active",
                "peak", "mae", "mins_to_actual_exit"]].to_string(index=False, float_format=lambda v: f"{v:,.2f}"))
    print()
    print("matched EXIT candidates: n=%d on %d days" % (len(E), E.date.nunique()))
    print("  improved (uplift>0): %d  worsened: %d" % ((E.uplift_pct > 0).sum(), (E.uplift_pct < 0).sum()))
    print("  mean uplift %+.3f%%p  median %+.3f%%p  total %s KRW" % (
        E.uplift_pct.mean(), E.uplift_pct.median(), format(E.uplift_krw.sum(), "+,.0f")))
    print("  runner wrongly cut (actual_net>bo_net and actual_net>0): %d" % (
        ((E.actual_net > E.bo_net) & (E.actual_net > 0)).sum()))
    sub = E[E.h50_active]
    print("  of which H50 HOLD already active at breakout: n=%d  mean uplift %+.3f%%p  total %s KRW" % (
        len(sub), sub.uplift_pct.mean() if len(sub) else float("nan"), format(sub.uplift_krw.sum(), "+,.0f")))
    sub2 = E[E.h50_held.astype(bool)]
    print("  of which trade ever hit H50 HOLD: n=%d  mean uplift %+.3f%%p  total %s KRW" % (
        len(sub2), sub2.uplift_pct.mean() if len(sub2) else float("nan"), format(sub2.uplift_krw.sum(), "+,.0f")))


if __name__ == "__main__":
    main()
