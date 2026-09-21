# -*- coding: utf-8 -*-
"""[M1] 오전 하이닉스 강도 = day-level regime signal 인가 — 정밀 검증. READ-ONLY.

section 1 : 일자별 feature + 조건부 확률
section 2 : 분위수 4그룹 (사후 임계 최적화 없음) + 앞39/뒤39 순서성
section 3 : 시간 안정성 09:30 / 09:45 / 10:00 / 10:15
section 4 : 방향성 분리 (RED/BLUE)

전부 완성봉 기반. cutoff 시각 이후 정보는 feature 에 쓰지 않는다.
"""
from __future__ import annotations
import sys
from collections import defaultdict
import numpy as np, pandas as pd
sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, r"C:\Users\FURSYS\Desktop\AI-GAP 2")
import k1_core as K
import g6_entry as E

A, D, W30, OOS = E.A, E.D, E.W30, E.OOS
B3 = E.B3
FLAG = E.FLAG
rng = np.random.default_rng(20260922)
CUTS = ("0930", "0945", "1000", "1015")

DAYB = {d: g for d, g in B3.groupby(B3["datetime"].dt.strftime("%Y%m%d"))}
TRD = defaultdict(list)
for t in A:
    TRD[t["date"]].append(t)


def morning(d, cut):
    """d 일의 09:00~cut 완성 3분봉 feature. 미래정보 없음."""
    b = DAYB.get(d)
    if b is None:
        return None
    m = b[(b["datetime"].dt.strftime("%H%M") >= "0900")
          & (b["datetime"].dt.strftime("%H%M") <= cut)]
    if len(m) < 4:
        return None
    o = float(m["open"].iloc[0])
    c = float(m["close"].iloc[-1])
    r = np.diff(np.log(pd.to_numeric(m["close"], errors="coerce").to_numpy()))
    idx = m.index
    nred = sum(1 for i in idx if str(FLAG.get(i, "")).endswith("UP_RED"))
    nblue = sum(1 for i in idx if str(FLAG.get(i, "")).endswith("DOWN_BLUE"))
    vall = float(b["volume"].mean())
    return {
        "ret": (c - o) / o * 100.0,                       # 부호 있는 누적
        "abs": abs(c - o) / o * 100.0,                    # |누적|
        "range": (float(m["high"].max()) - float(m["low"].min())) / o * 100.0,
        "rv": float(np.std(r, ddof=1) * np.sqrt(len(r)) * 100.0) if len(r) > 1 else np.nan,
        "volr": float(m["volume"].mean()) / vall if vall > 0 else np.nan,
        "red": nred, "blue": nblue, "flags": nred + nblue,
    }


# ── 일자 테이블 ────────────────────────────────────────────────────────
ROWS = []
for d in D:
    f = morning(d, "1000")
    if f is None:
        continue
    g = TRD.get(d, [])
    r = dict(date=d, **f)
    r["n"] = len(g)
    r["maxmfe"] = max((t["mfe"] for t in g), default=0.0)
    r["r3"] = int(any(t["mfe"] >= 3 for t in g))
    r["r5"] = int(any(t["mfe"] >= 5 for t in g))
    r["r8"] = int(any(t["mfe"] >= 8 for t in g))
    r["n3"] = sum(1 for t in g if t["mfe"] >= 3)
    r["pl"] = sum(t["pnl_krw"] for t in g)
    for c in CUTS:
        fc = morning(d, c)
        r["abs_" + c] = fc["abs"] if fc else np.nan
        r["ret_" + c] = fc["ret"] if fc else np.nan
    ROWS.append(r)
DF = pd.DataFrame(ROWS).set_index("date")


def auc(pos, neg):
    pos = [x for x in pos if x == x]; neg = [x for x in neg if x == x]
    if not pos or not neg:
        return float("nan")
    return sum((1.0 if p > n else 0.5 if p == n else 0.0)
               for p in pos for n in neg) / (len(pos) * len(neg))


if __name__ == "__main__":
    print("=" * 120)
    print(f"[M1] 오전 하이닉스 강도 = regime signal 정밀검증 — {len(DF)}영업일")
    print("=" * 120)
    print(f"  거래일 {int((DF['n']>0).sum())}일 / 무거래일 {int((DF['n']==0).sum())}일")
    print(f"  runner>=3% 있는 날 {int(DF['r3'].sum())}일 / >=5% {int(DF['r5'].sum())}일 / "
          f">=8% {int(DF['r8'].sum())}일")

    # ── 1. feature 별 판별력 ───────────────────────────────────────────
    print("\n" + "=" * 110)
    print("1. 오전 feature 의 runner 판별력 AUC (10:00 기준, 전체 78일)")
    print("=" * 110)
    FE = [("오전|누적|%", "abs"), ("오전 누적%(부호)", "ret"), ("오전 range%", "range"),
          ("오전 realized vol", "rv"), ("오전 거래량비", "volr"),
          ("오전 플래그수", "flags"), ("오전 RED수", "red"), ("오전 BLUE수", "blue")]
    print(f"  {'feature':18s} {'AUC(r3)':>8s} {'AUC(r5)':>8s} {'AUC(r8)':>8s} "
          f"{'r3날 평균':>10s} {'그외 평균':>10s}")
    print("  " + "-" * 70)
    for nm, k in FE:
        a3 = auc(DF.loc[DF.r3 == 1, k], DF.loc[DF.r3 == 0, k])
        a5 = auc(DF.loc[DF.r5 == 1, k], DF.loc[DF.r5 == 0, k])
        a8 = auc(DF.loc[DF.r8 == 1, k], DF.loc[DF.r8 == 0, k])
        print(f"  {nm:18s} {a3:8.3f} {a5:8.3f} {a8:8.3f} "
              f"{DF.loc[DF.r3==1, k].mean():10.3f} {DF.loc[DF.r3==0, k].mean():10.3f}")

    # ── 조건부 확률 ────────────────────────────────────────────────────
    print("\n  P(runner | 오전|누적| 분위) — 분위수 기준, 사후 임계 최적화 없음")
    q = DF["abs"].quantile([0.25, 0.5, 0.75]).tolist()
    DF["Q"] = pd.cut(DF["abs"], [-1e9] + q + [1e9], labels=["Q1약", "Q2", "Q3", "Q4강"])
    print(f"    {'구간':6s} {'|누적| 범위':>16s} {'일수':>4s} {'P(>=3%)':>8s} "
          f"{'P(>=5%)':>8s} {'P(>=8%)':>8s} {'평균 최대MFE':>11s}")
    for g in ["Q1약", "Q2", "Q3", "Q4강"]:
        s = DF[DF.Q == g]
        print(f"    {g:6s} {s['abs'].min():6.2f}~{s['abs'].max():6.2f}%  {len(s):4d} "
              f"{s['r3'].mean()*100:7.1f}% {s['r5'].mean()*100:7.1f}% {s['r8'].mean()*100:7.1f}% "
              f"{s['maxmfe'].mean():11.2f}")
    print(f"    {'전체':6s} {'':16s} {len(DF):4d} {DF['r3'].mean()*100:7.1f}% "
          f"{DF['r5'].mean()*100:7.1f}% {DF['r8'].mean()*100:7.1f}% {DF['maxmfe'].mean():11.2f}")

    # ── 2. 구간별 성과 ─────────────────────────────────────────────────
    print("\n" + "=" * 118)
    print("2. 분위 4그룹 성과 (전체 78일)")
    print("=" * 118)
    print(f"  {'구간':6s} {'일수':>4s} {'거래':>4s} {'실현P/L':>12s} {'평균/거래':>10s} "
          f"{'평균MFE':>8s} {'>=3%':>6s} {'>=5%':>6s} {'>=8%':>6s} {'PF':>6s} {'MDD%':>6s} "
          f"{'slot1':>9s} {'slot2':>9s} {'slot3':>9s}")
    print("  " + "-" * 114)
    for g in ["Q1약", "Q2", "Q3", "Q4강"]:
        days = list(DF[DF.Q == g].index)
        tr = [t for t in A if t["date"] in set(days)]
        if not tr:
            continue
        sl = [sum(t["pnl_krw"] for t in tr if t["slot"] == i) for i in (1, 2, 3)]
        print(f"  {g:6s} {len(days):4d} {len(tr):4d} {sum(t['pnl_krw'] for t in tr):12,.0f} "
              f"{sum(t['pnl_krw'] for t in tr)/len(tr):10,.0f} "
              f"{np.mean([t['mfe'] for t in tr]):8.2f} "
              f"{np.mean([t['mfe']>=3 for t in tr])*100:5.1f}% "
              f"{np.mean([t['mfe']>=5 for t in tr])*100:5.1f}% "
              f"{np.mean([t['mfe']>=8 for t in tr])*100:5.1f}% "
              f"{K.pf(A, days):6.3f} {K.mdd_krw(A, days):6.2f} "
              + " ".join(f"{x:9,.0f}" for x in sl))

    print("\n  앞39일 / 뒤39일 순서성 (같은 분위 경계를 각 창 안에서 다시 계산)")
    for tag, sub in (("앞39일", D[:39]), ("뒤39일", D[39:])):
        s = DF.loc[[d for d in sub if d in DF.index]].copy()
        qq = s["abs"].quantile([0.25, 0.5, 0.75]).tolist()
        s["Q2_"] = pd.cut(s["abs"], [-1e9] + qq + [1e9], labels=["Q1약", "Q2", "Q3", "Q4강"])
        out = []
        for g in ["Q1약", "Q2", "Q3", "Q4강"]:
            days = list(s[s.Q2_ == g].index)
            tr = [t for t in A if t["date"] in set(days)]
            out.append((g, len(days), len(tr), sum(t["pnl_krw"] for t in tr),
                        np.mean([t["mfe"] >= 3 for t in tr]) * 100 if tr else 0.0))
        print(f"    {tag}: " + " | ".join(
            f"{g} {nd}일/{nt}건 {pl:+,.0f} r3비율 {p3:.0f}%" for g, nd, nt, pl, p3 in out))

    # ── 3. 시간 안정성 ─────────────────────────────────────────────────
    print("\n" + "=" * 100)
    print("3. 시간 안정성 — cutoff 별 |누적| 의 판별력")
    print("=" * 100)
    print(f"  {'cutoff':>8s} {'AUC(r3)':>8s} {'AUC(r5)':>8s} {'AUC(r8)':>8s} "
          f"{'Q1약 r3비율':>11s} {'Q4강 r3비율':>11s} {'Q4-Q1 평균MFE':>13s}")
    print("  " + "-" * 82)
    for c in CUTS:
        k = "abs_" + c
        s = DF.dropna(subset=[k]).copy()
        qq = s[k].quantile([0.25, 0.5, 0.75]).tolist()
        s["QQ"] = pd.cut(s[k], [-1e9] + qq + [1e9], labels=["Q1", "Q2", "Q3", "Q4"])
        lo, hi = s[s.QQ == "Q1"], s[s.QQ == "Q4"]
        print(f"  {c[:2]}:{c[2:]:>4s} {auc(s.loc[s.r3==1,k], s.loc[s.r3==0,k]):8.3f} "
              f"{auc(s.loc[s.r5==1,k], s.loc[s.r5==0,k]):8.3f} "
              f"{auc(s.loc[s.r8==1,k], s.loc[s.r8==0,k]):8.3f} "
              f"{lo['r3'].mean()*100:10.1f}% {hi['r3'].mean()*100:10.1f}% "
              f"{hi['maxmfe'].mean()-lo['maxmfe'].mean():13.2f}")

    # ── 4. 방향성 ──────────────────────────────────────────────────────
    print("\n" + "=" * 112)
    print("4. 방향성 분리 — 오전 누적수익률 부호 x 진입방향 (10:00 기준, 분위 경계 |ret|)")
    print("=" * 112)
    thr = DF["abs"].quantile(0.5)
    def band(d):
        v = DF.loc[d, "ret"] if d in DF.index else np.nan
        if v != v or abs(v) < thr:
            return "약함"
        return "상승강" if v > 0 else "하락강"
    print(f"  (강/약 경계 = |누적| 중앙값 {thr:.3f}%)")
    print(f"  {'오전상태':8s} {'방향':9s} {'거래':>4s} {'평균net%':>8s} {'승률':>6s} "
          f"{'평균MFE':>8s} {'>=3%':>6s} {'실현P/L':>12s}")
    print("  " + "-" * 76)
    for bnd in ("상승강", "하락강", "약함"):
        for dr in ("UP_RED", "DOWN_BLUE"):
            g = [t for t in A if band(t["date"]) == bnd and t["direction"] == dr]
            if not g:
                print(f"  {bnd:8s} {dr:9s} (없음)"); continue
            print(f"  {bnd:8s} {dr:9s} {len(g):4d} "
                  f"{np.mean([t['net_pct'] for t in g]):+8.3f} "
                  f"{np.mean([t['net_pct']>0 for t in g])*100:5.1f}% "
                  f"{np.mean([t['mfe'] for t in g]):8.2f} "
                  f"{np.mean([t['mfe']>=3 for t in g])*100:5.1f}% "
                  f"{sum(t['pnl_krw'] for t in g):12,.0f}")
