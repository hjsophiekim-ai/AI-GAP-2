"""CHOP 사이징 축소 판정 (2026-10-18). READ-ONLY. 실제 하네스 재생 원장.

A 현행 1.00 / B 0.80 / C 0.60  — CHOP 라벨 진입의 주문 배수에만 계수를 곱한다.
진입 로직(게이트·슬롯)과 청산 래더는 그대로다.

주의: production 의 X2LITE_SIZING_CHOP_MULT(0.80) 는 **CHOP 모드에서 돌지 않는다**
(position_sizing.is_active 가 W1A 계열에서만 True). 그래서 CHOP 모드 현행은 1.00 이고,
0.80 도 '축소안' 중 하나다.
"""
import json
import os
import sys

import numpy as np
import pandas as pd

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import rep2  # noqa: E402

CAP = 1e7
TAGS = [("A 현행 1.00", "CHOP"), ("B 0.80", "CSZ80"), ("C 0.60", "CSZ60")]


def load(tag, dys):
    rows = []
    for d in dys:
        p = f"{rep2.LED}/{tag}_{d}.json"
        if not os.path.exists(p):
            return None
        for t in rep2.trades(json.load(open(p, encoding="utf-8"))):
            rows.append(dict(day=d, entry=t["entry"].strftime("%H:%M"), sym=t["sym"],
                             krw=t["krw"], net=t["net"], reason="+".join(t["reasons"])))
    return pd.DataFrame(rows)


def strk(a):
    b = c = 0
    for v in a:
        c = c + 1 if v < 0 else 0
        b = max(b, c)
    return b


def met(tr, dys):
    s = tr.groupby("day").krw.sum() if len(tr) else pd.Series(dtype=float)
    dp = np.array([float(s.get(d, 0.0)) for d in dys])
    k = tr.krw.to_numpy() if len(tr) else np.array([])
    eq = np.cumprod(1 + dp / CAP)
    mdd = float((eq / np.maximum.accumulate(np.r_[1.0, eq])[1:] - 1).min() * 100)
    top = np.sort(dp)[::-1]
    return dict(tot=dp.sum(), n=len(k), win=100 * (k > 0).mean() if len(k) else 0,
                pf=(k[k > 0].sum() / max(-k[k < 0].sum(), 1)) if len(k) else 0, mdd=mdd,
                worst=dp.min(), lossd=int((dp < 0).sum()), strk=strk(dp),
                losssum=dp[dp < 0].sum(), winsum=dp[dp > 0].sum(),
                x1=dp.sum() - top[:1].sum(), x2=dp.sum() - top[:2].sum(), dp=dp)


def main():
    days = [x.strip() for x in open(rep2.MY + "/days.txt") if x.strip()]
    for lab, sub in (("최근 16영업일", days[-16:]), ("최근 10영업일", days[-10:])):
        sets = {n: load(t, sub) for n, t in TAGS}
        if any(v is None for v in sets.values()):
            print(f"[{lab}] 원장 미완 — 건너뜀")
            continue
        print("=" * 118)
        print(f"== {lab} ({sub[0]}~{sub[-1]})")
        print("=" * 118)
        print(f"  {'사이즈':12s} {'총손익':>12s} {'거래':>4s} {'승률':>6s} {'PF':>5s} {'MDD':>7s} "
              f"{'최대1일손실':>12s} {'손실일':>5s} {'최대연속':>5s} {'손실일합':>12s} {'이익일합':>12s}")
        b = met(sets["A 현행 1.00"], sub)
        for n, tr in sets.items():
            m = met(tr, sub)
            print(f"  {n:12s} {m['tot']:+12,.0f} {m['n']:4d} {m['win']:5.1f}% {m['pf']:5.2f} "
                  f"{m['mdd']:7.2f} {m['worst']:+12,.0f} {m['lossd']:5d} {m['strk']:5d} "
                  f"{m['losssum']:+12,.0f} {m['winsum']:+12,.0f}")
        print(f"\n  {'사이즈':12s} {'Δ총손익':>12s} {'Δ대비 수익유지':>12s} {'Δ최대1일손실':>14s} "
              f"{'ΔMDD':>8s} {'상위1제외':>12s} {'상위2제외':>12s}")
        for n, tr in sets.items():
            m = met(tr, sub)
            keep = (m["tot"] / b["tot"] * 100) if b["tot"] else float("nan")
            print(f"  {n:12s} {m['tot'] - b['tot']:+12,.0f} {keep:11.1f}% "
                  f"{m['worst'] - b['worst']:+14,.0f} {m['mdd'] - b['mdd']:+8.2f} "
                  f"{m['x1']:+12,.0f} {m['x2']:+12,.0f}")
        print("\n  [일별]")
        t = pd.DataFrame({"일자": sub})
        for n, tr in sets.items():
            t[n] = [round(met(tr[tr.day == d], [d])["tot"]) for d in sub]
        print(t.to_string(index=False))
        print()


if __name__ == "__main__":
    main()
