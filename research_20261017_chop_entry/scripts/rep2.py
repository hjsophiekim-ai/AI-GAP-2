"""CHOP 기준 변형 비교표 (2026-10-17). READ-ONLY.

사용: python rep2.py [태그...]        (기본: CEMAOFF CPE05 CPE10 CEMAPE)
기준: CHOP (= E + 왕복장방어 3종 + SLOW-TP2). 참고로 E(BASE) 도 같이 찍는다.
변형 원장이 없는 날은 CHOP 원장을 쓴다 — 그 날은 규칙이 건드릴 것이 없어 정의상 동일하다.
"""
import glob
import json
import os
import sys

import numpy as np
import pandas as pd

sys.stdout.reconfigure(encoding="utf-8")
LED = "C:/Users/KIMHYU~1/AppData/Local/Temp/claude/G----------------2--Desktop-AI-GAP-2/e202387f-ec1a-41fa-a078-e46b72ece959/scratchpad/wk/out"
MY = "C:/Users/KIMHYU~1/AppData/Local/Temp/claude/G----------------2--Desktop-AI-GAP-2/e202387f-ec1a-41fa-a078-e46b72ece959/scratchpad/wk"
CAP = 10_000_000.0
BIGWIN = 300_000
pd.set_option("display.width", 250)


def trades(o):
    sells = [x for x in o["orders"] if x["side"] == "SELL"]
    ex_s = [e for e in o["ex"] if e["side"] == "SELL"][-len(sells):] if sells else []
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
                       sold=0, krw=0.0, reasons=[], exit=None, sym=x["sym"])
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
            cur = None
    return [t for t in out if "net" in t]


def days():
    return [x.strip() for x in open(MY + "/days.txt") if x.strip()]


def load(tag, fb="CHOP"):
    rows, miss = [], 0
    for d in days():
        p = f"{LED}/{tag}_{d}.json"
        if not os.path.exists(p):
            p = f"{LED}/{fb}_{d}.json"
            miss += 1
        if not os.path.exists(p):
            continue
        for t in trades(json.load(open(p, encoding="utf-8"))):
            rows.append(dict(day=d, sym=t["sym"], entry=t["entry"].strftime("%H:%M"),
                             krw=t["krw"], net=t["net"], reason="+".join(t["reasons"])))
    return pd.DataFrame(rows), miss


def metrics(tr, dys):
    s = tr.groupby("day").krw.sum() if len(tr) else pd.Series(dtype=float)
    dp = np.array([float(s.get(d, 0.0)) for d in dys])
    k = tr.krw.to_numpy() if len(tr) else np.array([])
    eq = np.cumprod(1 + dp / CAP)
    mdd = float((eq / np.maximum.accumulate(np.r_[1.0, eq])[1:] - 1).min() * 100) if len(eq) else 0.0
    pf = (k[k > 0].sum() / max(-k[k < 0].sum(), 1.0)) if len(k) else 0.0
    top = np.sort(dp)[::-1]
    return dict(tot=dp.sum(), n=len(k), win=(100 * (k > 0).mean() if len(k) else 0),
                avg=(k.mean() if len(k) else 0), pf=pf, mdd=mdd,
                lossd=int((dp < 0).sum()), worst=float(dp.min()) if len(dp) else 0,
                x1=dp.sum() - top[:1].sum(), x2=dp.sum() - top[:2].sum(), x3=dp.sum() - top[:3].sum())


def main():
    tags = sys.argv[1:] or ["CEMAOFF", "CPE05", "CPE10", "CEMAPE"]
    dys = days()
    have = {t: len(glob.glob(f"{LED}/{t}_*.json")) for t in ["CHOP"] + tags}
    print("원장 보유:", ", ".join(f"{k} {v}" for k, v in have.items()), f"/ 대상 {len(dys)}일\n")
    sets = {"E (현행 production)": load("BASE", "BASE")[0], "CHOP (기준선)": load("CHOP", "CHOP")[0]}
    for t in tags:
        if have.get(t):
            sets[f"CHOP+{t}"] = load(t)[0]
    base = sets["CHOP (기준선)"]

    wins = [("89일 전체", dys), ("5~7월", [d for d in dys if d < "20260801"]),
            ("8~10월", [d for d in dys if d >= "20260801"]), ("최근 10일", dys[-10:])]
    for lab, sub in wins:
        print("=" * 120)
        print(f"== {lab} ({len(sub)}일)")
        print("=" * 120)
        print(f"  {'전략':22s} {'총손익':>13s} {'거래':>4s} {'승률':>6s} {'평균':>10s} {'PF':>5s} "
              f"{'MDD':>7s} {'손실일':>5s} {'최대1일손실':>12s} {'ΔCHOP':>12s}")
        bm = metrics(base[base.day.isin(sub)], sub)
        for name, tr in sets.items():
            m = metrics(tr[tr.day.isin(sub)] if len(tr) else tr, sub)
            dd = "" if name.startswith("CHOP (") else f"{m['tot'] - bm['tot']:+12,.0f}"
            print(f"  {name:22s} {m['tot']:+13,.0f} {m['n']:4d} {m['win']:5.1f}% {m['avg']:+10,.0f} "
                  f"{m['pf']:5.2f} {m['mdd']:7.2f} {m['lossd']:5d} {m['worst']:+12,.0f} {dd:>12s}")
        print(f"\n  {'전략':22s} {'상위1일 제외':>14s} {'상위2일 제외':>14s} {'상위3일 제외':>14s}")
        for name, tr in sets.items():
            m = metrics(tr[tr.day.isin(sub)] if len(tr) else tr, sub)
            print(f"  {name:22s} {m['x1']:+14,.0f} {m['x2']:+14,.0f} {m['x3']:+14,.0f}")
        print()

    for name, tr in sets.items():
        if name.startswith(("E ", "CHOP (")):
            continue
        print("=" * 120)
        print(f"===== {name} 상세 (기준 CHOP)")
        print("=" * 120)
        kb = base.set_index(["day", "entry", "sym"]).krw
        kv = tr.set_index(["day", "entry", "sym"]).krw
        gone = base[~base.set_index(["day", "entry", "sym"]).index.isin(kv.index)]
        new = tr[~tr.set_index(["day", "entry", "sym"]).index.isin(kb.index)]
        L = gone[gone.krw < 0]
        print(f"  막은 거래 {len(gone)}건 합 {gone.krw.sum():+,.0f}  "
              f"(그중 손실 {len(L)}건 {L.krw.sum():+,.0f} = 막은 손실, "
              f"이익 {len(gone) - len(L)}건 {gone[gone.krw > 0].krw.sum():+,.0f})")
        big = gone[gone.krw >= BIGWIN]
        print(f"  놓친 큰 승자(+30만 이상) {len(big)}건 {big.krw.sum():+,.0f}")
        if len(big):
            print(big[["day", "entry", "sym", "krw", "net", "reason"]].round(2).to_string(index=False))
        print(f"  새로 생긴 거래 {len(new)}건 합 {new.krw.sum():+,.0f} "
              f"승률 {(new.krw > 0).mean() * 100 if len(new) else 0:.0f}%")
        if len(new):
            print(new[["day", "entry", "sym", "krw", "net", "reason"]].round(2).to_string(index=False))
        r10 = dys[-10:]
        g10, n10 = gone[gone.day.isin(r10)], new[new.day.isin(r10)]
        print(f"  [최근 10일] 막은 거래 {len(g10)}건 {g10.krw.sum():+,.0f} "
              f"(손실 {len(g10[g10.krw < 0])}건 {g10[g10.krw < 0].krw.sum():+,.0f}) / "
              f"새 거래 {len(n10)}건 {n10.krw.sum():+,.0f}")
        sb = base.groupby("day").krw.sum()
        sv = tr.groupby("day").krw.sum()
        diff = (sv.reindex(dys).fillna(0) - sb.reindex(dys).fillna(0))
        ch = diff[diff.abs() > 1]
        rng = np.random.default_rng(0)
        for lab2, sub2 in (("전체", dys), ("8~10월", [d for d in dys if d >= "20260801"]),
                           ("최근10일", dys[-10:])):
            x = diff[sub2].to_numpy()
            bs = [rng.choice(x, len(x), replace=True).sum() for _ in range(4000)]
            print(f"  bootstrap {lab2:7s} Δ {x.sum():+12,.0f}  95% [{np.percentile(bs, 2.5):+,.0f}, "
                  f"{np.percentile(bs, 97.5):+,.0f}]  P(Δ>0) {np.mean(np.array(bs) > 0) * 100:3.0f}%")
        print(f"  달라진 날 {len(ch)}일")
        if len(ch):
            print(pd.DataFrame(dict(CHOP=sb.reindex(ch.index).fillna(0).round(0),
                                    VAR=sv.reindex(ch.index).fillna(0).round(0),
                                    Δ=ch.round(0))).to_string())
        print()


if __name__ == "__main__":
    main()
