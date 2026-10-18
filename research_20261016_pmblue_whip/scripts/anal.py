"""재생 원장 -> 지표. READ-ONLY 공용 모듈 (2026-10-16).

거래 묶기(trades)는 research_20261004_chop_staged/an8.py -> bb/feat.py 의 것을 그대로 쓴다.
변형 원장이 없는 날은 BASE 원장을 그대로 쓴다 — 그 날은 규칙이 건드릴 것이 없어
A/B 가 정의상 동일하기 때문이다(해당 조건이 성립한 날만 재생한다).
"""
import glob
import json
import os

import numpy as np
import pandas as pd

MY = "C:/Users/KIMHYU~1/AppData/Local/Temp/claude/G----------------2--Desktop-AI-GAP-2/e202387f-ec1a-41fa-a078-e46b72ece959/scratchpad/wk"
OUT = MY + "/out"
CAP = 10_000_000.0


def trades(o):
    sells = [x for x in o["orders"] if x["side"] == "SELL"]
    ex_s = [e for e in o["ex"] if e["side"] == "SELL"][-len(sells):] if sells else []
    assert len(ex_s) == len(sells), (o["D"], o["VAR"])
    exe = {pd.Timestamp(s["detected_at"]).strftime("%H:%M:%S"): s["signal_id"]
           for s in o["sig"] if s["order_result"] == "EXECUTED"}
    out, cur, si = [], None, 0
    for x in o["orders"]:
        t = pd.Timestamp(x["t"])
        if x["side"] == "BUY":
            if cur is not None and cur["sold"] < cur["q"]:
                tot = cur["q"] + x["qty"]
                cur["px_in"] = (cur["px_in"] * cur["q"] + x["px"] * x["qty"]) / tot
                cur["q"] = tot
                continue
            cur = dict(day=o["D"], entry=t, px_in=float(x["px"]), q=int(x["qty"]),
                       sid=exe.get(t.strftime("%H:%M:%S"), ""), sold=0, krw=0.0,
                       reasons=[], exit=None, sym=x["sym"])
            out.append(cur)
            continue
        e = ex_s[si]
        si += 1
        if cur is None:
            continue
        cur["sold"] += x["qty"]
        cur["krw"] += float(e["net_pnl"] or 0)
        cur["reasons"].append(e["exit_reason"] or "?")
        cur["exit"] = t
        if cur["sold"] >= cur["q"]:
            cur["net"] = cur["krw"] / (cur["q"] * cur["px_in"]) * 100
            cur["hold"] = (cur["exit"] - cur["entry"]).total_seconds() / 60
            cur = None
    return [t for t in out if "net" in t]


def days():
    return [x.strip() for x in open(MY + "/days.txt") if x.strip()]


def load(tag, fallback="BASE"):
    """tag 원장 + 없는 날은 fallback 원장. -> DataFrame(거래 단위)"""
    rows = []
    for d in days():
        p = f"{OUT}/{tag}_{d}.json"
        if not os.path.exists(p):
            p = f"{OUT}/{fallback}_{d}.json"
        if not os.path.exists(p):
            continue
        o = json.load(open(p, encoding="utf-8"))
        for t in trades(o):
            rows.append(dict(day=d, sid=t["sid"], sym=t["sym"], entry=t["entry"].strftime("%H:%M"),
                             exit=t["exit"].strftime("%H:%M"), krw=t["krw"], net=t["net"],
                             hold=t["hold"], reason="+".join(t["reasons"])))
    return pd.DataFrame(rows)


def daily(tr, dys=None):
    dys = dys or days()
    s = tr.groupby("day").krw.sum() if len(tr) else pd.Series(dtype=float)
    return np.array([float(s.get(d, 0.0)) for d in dys])


def metrics(tr, dys=None):
    dys = dys or days()
    dp = daily(tr, dys)
    k = tr.krw.to_numpy() if len(tr) else np.array([])
    r = dp / CAP
    eq = np.cumprod(1 + r)
    mdd = float((eq / np.maximum.accumulate(np.r_[1.0, eq])[1:] - 1).min() * 100) if len(eq) else 0.0
    pf = (k[k > 0].sum() / max(-k[k < 0].sum(), 1.0)) if len(k) else 0.0
    top = np.sort(dp)[::-1]
    return dict(
        총손익=dp.sum(), 거래=len(k), 승률=(100 * (k > 0).mean() if len(k) else 0.0),
        평균수익=(k.mean() if len(k) else 0.0), 평균net=(tr.net.mean() if len(tr) else 0.0),
        PF=pf, MDD=mdd, 손실일=int((dp < 0).sum()), 최대1일손실=float(dp.min()) if len(dp) else 0.0,
        상위1제외=dp.sum() - top[:1].sum(), 상위2제외=dp.sum() - top[:2].sum(),
        상위3제외=dp.sum() - top[:3].sum(),
    )


def fmt(name, m):
    return (f"{name:16s} {m['총손익']:+12,.0f} 거래 {m['거래']:3d} 승률 {m['승률']:5.1f}% "
            f"평균 {m['평균수익']:+9,.0f}({m['평균net']:+5.2f}%) PF {m['PF']:5.2f} MDD {m['MDD']:6.2f} "
            f"손실일 {m['손실일']:2d} 최악일 {m['최대1일손실']:+10,.0f}")


def fmt_top(name, m):
    return (f"{name:16s} 상위1제외 {m['상위1제외']:+12,.0f} 상위2제외 {m['상위2제외']:+12,.0f} "
            f"상위3제외 {m['상위3제외']:+12,.0f}")


def halves(tr):
    dys = days()
    return (("전체", tr, dys),
            ("5~7월", tr[tr.day < "20260801"] if len(tr) else tr, [d for d in dys if d < "20260801"]),
            ("8~10월", tr[tr.day >= "20260801"] if len(tr) else tr, [d for d in dys if d >= "20260801"]))


def big_winner_check(base, var, floor=300_000):
    """BASE 의 큰 승자가 변형에서 사라졌/줄었는지."""
    b = base[base.krw >= floor].copy()
    rows = []
    for r in b.itertuples():
        same = var[(var.day == r.day) & (var.entry == r.entry) & (var.sym == r.sym)]
        got = float(same.krw.sum()) if len(same) else 0.0
        rows.append(dict(day=r.day, entry=r.entry, sym=r.sym, base=r.krw, var=got, delta=got - r.krw,
                         status=("유지" if len(same) and abs(got - r.krw) < 1 else
                                 ("사라짐" if not len(same) else "변함"))))
    return pd.DataFrame(rows)
