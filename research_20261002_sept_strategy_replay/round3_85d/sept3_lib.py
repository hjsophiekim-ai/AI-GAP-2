"""9월 2차 비교 집계: A P3-R0 / B D-R0 / C D-NOH50 / D P3-TREND-BYPASS (전부 production worker 전체 재생, REAL 체결). READ-ONLY.
손익 = production 거래원장 net_pnl. 일 수익률 = 원 / 1,000만원.
"""
import json, os, sys
import numpy as np
import pandas as pd
sys.stdout.reconfigure(encoding="utf-8")
S = sys.argv[1] if len(sys.argv) > 1 else ""
O2, O3 = S + "/wk/out2", S + "/wk/out3"
SEPT = ["20260903", "20260904", "20260907", "20260908", "20260909", "20260910", "20260911", "20260914", "20260915",
        "20260916", "20260917", "20260918", "20260921", "20260922", "20260923", "20260928", "20260929", "20260930"]
RECENT = ["20260922", "20260923", "20260928", "20260929", "20260930", "20261001"]
ALL = SEPT + ["20261001"]
LONG = "0193T0"
SRC = {"A": (O3, "A"), "B": (O2, "D"), "C": (O3, "D0"), "D": (O3, "E")}
NAME = {"A": "A P3-R0", "B": "B D-R0 (SIMPLE-N1-H50-NOSTOP)", "C": "C D-NOH50", "D": "D P3-TREND-BYPASS"}


def load(k, d):
    o, v = SRC[k]
    return json.load(open(f"{o}/REAL_{v}_{d}.json", encoding="utf-8"))


def trades(o):
    sells = [x for x in o["orders"] if x["side"] == "SELL"]
    ex_s = [e for e in o["ex"] if e["side"] == "SELL"][-len(sells):] if sells else []
    assert len(ex_s) == len(sells), (o["D"], o["VAR"])
    h50 = [pd.Timestamp(s["detected_at"]) for s in o["sig"] if (s["order_result"] or "") == "H50_SMALL_WHIPSAW_HOLD"]
    exe = {pd.Timestamp(s["detected_at"]).strftime("%H:%M:%S"): s["signal_id"] for s in o["sig"] if s["order_result"] == "EXECUTED"}
    ent = {pd.Timestamp(e["t"]).strftime("%H:%M:%S"): e for e in o.get("entries", [])}
    out, cur, si = [], None, 0
    for x in o["orders"]:
        t = pd.Timestamp(x["t"])
        if x["side"] == "BUY":
            sid = exe.get(t.strftime("%H:%M:%S"), "")
            e = ent.get(t.strftime("%H:%M:%S"), {})
            cur = dict(day=o["D"], entry=t, dir="UP 레버" if x["sym"] == LONG else "DN 인버", px_in=x["px"], q=x["qty"],
                       flag=(sid.split("_")[1][:4] if sid else "?"), sold=0, legs=[], krw=0.0, reasons=[], exit=None,
                       regime=e.get("regime"), trend_ok=e.get("trend_ok"), bypass=e.get("bypass", False))
            out.append(cur); continue
        e = ex_s[si]; si += 1
        if cur is None:
            continue
        cur["sold"] += x["qty"]; cur["legs"].append((t, x["qty"], x["px"]))
        cur["krw"] += float(e["net_pnl"] or 0); cur["reasons"].append(e["exit_reason"] or "?"); cur["exit"] = t
        if cur["sold"] >= cur["q"]:
            cur["px_out"] = sum(q * p for _, q, p in cur["legs"]) / cur["q"]
            cur["net"] = cur["krw"] / (cur["q"] * cur["px_in"]) * 100
            cur["hold_min"] = (cur["exit"] - cur["entry"]).total_seconds() / 60
            cur["h50"] = any(cur["entry"] < h <= cur["exit"] for h in h50)
            cur["h50_at"] = next((h for h in h50 if cur["entry"] < h <= cur["exit"]), None)
            cur = None
    return [t for t in out if "net" in t]


def metrics(days, tr):
    daily = np.array([sum(t["krw"] for t in tr if t["day"] == d) for d in days])
    r = daily / 10_000_000 * 100
    eq = np.cumprod(1 + r / 100)
    k = np.array([t["krw"] for t in tr]) if tr else np.zeros(1)
    return dict(krw=daily.sum(), comp=(eq[-1] - 1) * 100, pf=k[k > 0].sum() / max(-k[k < 0].sum(), 1.0),
                mdd=(eq / np.maximum.accumulate(np.r_[1.0, eq])[1:] - 1).min() * 100, win=(k > 0).mean() * 100,
                lossd=int((daily < 0).sum()), d2=int((r >= 2).sum()), n=len(tr),
                hold=np.mean([t["hold_min"] for t in tr]), dmin=daily.min(), dmax=daily.max())


def key(t):
    return (t["day"], t["entry"], t["dir"])


def rs(t):
    return "+".join(dict.fromkeys(t["reasons"]))


