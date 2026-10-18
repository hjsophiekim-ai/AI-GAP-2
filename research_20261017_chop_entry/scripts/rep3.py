"""청산 오버레이 4후보 비교 (2026-10-18). READ-ONLY. 전부 실제 하네스 재생 원장.

A = CHOP          기준선
B = CHOP + TP1.5 / SL-1.5   (현재 토글 규칙)
C = CHOP + TP3.0 / SL-1.5   (큰 승자 보존 + 손절 소폭 확대)
D = CHOP + TP3.0 / SL-1.0   (현행 손절 유지 + 익절만 확대)

판정 기준 (사용자 지정):
  1) CHOP 보다 총손익 증가   2) MDD 악화가 크지 않을 것   3) 최대1일손실 과도 악화 없음
  4) 특정 1~2일 의존 아님    5) 8~10월 전체에서 우위 유지
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
BIG = 300_000
TAGS = [("A CHOP 기준선", "CHOP"), ("B TP1.5/SL-1.5", "OVB"),
        ("C TP3.0/SL-1.5", "OVC"), ("D TP3.0/SL-1.0", "OVD")]


def load(tag, dys):
    rows = []
    for d in dys:
        p = f"{rep2.LED}/{tag}_{d}.json"
        if not os.path.exists(p):
            continue
        for t in rep2.trades(json.load(open(p, encoding="utf-8"))):
            rows.append(dict(day=d, entry=t["entry"].strftime("%H:%M"), sym=t["sym"],
                             krw=t["krw"], net=t["net"], reason="+".join(t["reasons"])))
    return pd.DataFrame(rows)


def streak(a):
    best = cur = 0
    for v in a:
        cur = cur + 1 if v < 0 else 0
        best = max(best, cur)
    return best


def met(tr, dys):
    s = tr.groupby("day").krw.sum() if len(tr) else pd.Series(dtype=float)
    dp = np.array([float(s.get(d, 0.0)) for d in dys])
    k = tr.krw.to_numpy() if len(tr) else np.array([])
    eq = np.cumprod(1 + dp / CAP)
    mdd = float((eq / np.maximum.accumulate(np.r_[1.0, eq])[1:] - 1).min() * 100) if len(eq) else 0
    top = np.sort(dp)[::-1]
    return dict(tot=dp.sum(), n=len(k), win=(100 * (k > 0).mean() if len(k) else 0),
                pf=(k[k > 0].sum() / max(-k[k < 0].sum(), 1)) if len(k) else 0, mdd=mdd,
                worst=dp.min() if len(dp) else 0, lossd=int((dp < 0).sum()), strk=streak(dp),
                losssum=dp[dp < 0].sum(), big=int((k >= BIG).sum()), bigsum=k[k >= BIG].sum(),
                x1=dp.sum() - top[:1].sum(), x2=dp.sum() - top[:2].sum(), x3=dp.sum() - top[:3].sum(),
                dp=dp)


def main():
    days = [x.strip() for x in open(rep2.MY + "/days.txt") if x.strip()]
    rec = [d for d in days if d >= "20260901"]      # 9~10월만 (사용자 요청)
    have = {t: sum(1 for d in rec if os.path.exists(f"{rep2.LED}/{t}_{d}.json")) for _, t in TAGS}
    print(f"원장 보유 (9~10월 {len(rec)}일 기준):", ", ".join(f"{t} {v}" for t, v in have.items()), "\n")
    sets = {}
    for name, tag in TAGS:
        if have[tag] == 0:
            continue
        sets[name] = load(tag, rec)

    for lab, sub in ((f"9~10월 {len(rec)}일", rec), ("최근 20일", days[-20:]),
                     ("최근 16일", days[-16:]), ("최근 10일", days[-10:])):
        print("=" * 128)
        print(f"== {lab} ({sub[0]}~{sub[-1]}, {len(sub)}일)")
        print("=" * 128)
        print(f"  {'후보':18s} {'총손익':>13s} {'거래':>4s} {'승률':>6s} {'PF':>5s} {'MDD':>7s} "
              f"{'최대1일손실':>12s} {'손실일':>5s} {'최대연속':>5s} {'손실일합':>13s} {'큰승자':>5s}")
        base = None
        for name, tr in sets.items():
            m = met(tr[tr.day.isin(sub)] if len(tr) else tr, sub)
            if base is None:
                base = m
            print(f"  {name:18s} {m['tot']:+13,.0f} {m['n']:4d} {m['win']:5.1f}% {m['pf']:5.2f} "
                  f"{m['mdd']:7.2f} {m['worst']:+12,.0f} {m['lossd']:5d} {m['strk']:5d} "
                  f"{m['losssum']:+13,.0f} {m['big']:5d}")
        print(f"\n  {'후보':18s} {'ΔCHOP':>13s} {'상위1 제외':>13s} {'상위2 제외':>13s} {'상위3 제외':>13s} "
              f"{'상위3제외 ΔCHOP':>16s}")
        for name, tr in sets.items():
            m = met(tr[tr.day.isin(sub)] if len(tr) else tr, sub)
            print(f"  {name:18s} {m['tot'] - base['tot']:+13,.0f} {m['x1']:+13,.0f} {m['x2']:+13,.0f} "
                  f"{m['x3']:+13,.0f} {m['x3'] - base['x3']:+16,.0f}")
        print()

    # 판정표 (8~10월 기준)
    print("=" * 128)
    print(f"== 판정 (9~10월 {len(rec)}일 기준, 사용자 지정 5조건)")
    print("=" * 128)
    b = met(sets["A CHOP 기준선"], rec)
    rng = np.random.default_rng(0)
    for name, tr in sets.items():
        if name.startswith("A "):
            continue
        m = met(tr, rec)
        d = m["dp"] - b["dp"]
        bs = [rng.choice(d, len(d), replace=True).sum() for _ in range(4000)]
        c1 = m["tot"] > b["tot"]
        c2 = m["mdd"] >= b["mdd"] - 1.0
        c3 = m["worst"] >= b["worst"] * 1.25
        c4 = (m["x3"] - b["x3"]) > 0
        c5 = c1 and c4
        ok = lambda v: "O" if v else "X"  # noqa: E731
        print(f"\n  [{name}]  Δ {m['tot'] - b['tot']:+,.0f}  "
              f"bootstrap 95% [{np.percentile(bs, 2.5):+,.0f}, {np.percentile(bs, 97.5):+,.0f}]  "
              f"P(Δ>0) {np.mean(np.array(bs) > 0) * 100:.0f}%")
        print(f"    1 총손익 증가            {ok(c1)}  {b['tot']:+,.0f} -> {m['tot']:+,.0f}")
        print(f"    2 MDD 악화 1%p 이내      {ok(c2)}  {b['mdd']:.2f} -> {m['mdd']:.2f}")
        print(f"    3 최대1일손실 25% 이내 악화 {ok(c3)}  {b['worst']:+,.0f} -> {m['worst']:+,.0f}")
        print(f"    4 상위3일 제외해도 우위   {ok(c4)}  {b['x3']:+,.0f} -> {m['x3']:+,.0f}")
        print(f"    5 9~10월 전체 우위        {ok(c5)}")
        print(f"    (참고) 최대연속손실일 {b['strk']} -> {m['strk']},  손실일 {b['lossd']} -> {m['lossd']},  "
              f"큰승자 {b['big']}건 {b['bigsum']:+,.0f} -> {m['big']}건 {m['bigsum']:+,.0f}")


if __name__ == "__main__":
    main()
