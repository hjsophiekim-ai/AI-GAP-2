# -*- coding: utf-8 -*-
"""[M2] (a) AUC 0.692 의 출처 규명  (b) 방향성 결합을 **인과적으로** 재측정.

m1 section4 의 문제: 오전(09:00~10:00) 상태를 10:00 **이전에 진입한 거래**에도
적용했다 = 그 거래들에겐 미래정보다. 여기서는 두 방식으로 바로잡는다.
  방식1  cutoff(10:00) 이후 진입한 거래에만 적용
  방식2  각 거래의 **진입 직전 완성봉까지**의 당일 누적으로 판정 (전 거래 인과)
READ-ONLY.
"""
from __future__ import annotations
import sys
from collections import defaultdict
import numpy as np, pandas as pd
sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, r"C:\Users\FURSYS\Desktop\AI-GAP 2")
import k1_core as K
import g6_entry as E
import m1_morning as M

A, D, W30, OOS = E.A, E.D, E.W30, E.OOS
B3, DAYB = E.B3, M.DAYB
rng = np.random.default_rng(20260922)

# ══ (a) AUC 0.692 의 출처 — 09:00 하한 유무 ═══════════════════════════
print("=" * 112)
print("(a) 앞선 보고의 AUC 0.692 출처 규명")
print("=" * 112)
DMAX = defaultdict(float)
for t in A:
    DMAX[t["date"]] = max(DMAX[t["date"]], t["mfe"])


def am_abs(d, lower):
    b = DAYB.get(d)
    if b is None:
        return np.nan
    hh = b["datetime"].dt.strftime("%H%M")
    m = b[(hh <= "1000") & (hh >= lower)] if lower else b[hh <= "1000"]
    if len(m) < 2:
        return np.nan
    o = float(m["open"].iloc[0])
    return abs(float(m["close"].iloc[-1]) - o) / o * 100.0


for lab, lower in (("08:00~10:00 (하한 없음 = 앞선 보고)", ""),
                   ("09:00~10:00 (정규장만)", "0900")):
    v = {d: am_abs(d, lower) for d in D}
    pos = [v[d] for d in D if DMAX[d] >= 3.0 and v[d] == v[d]]
    neg = [v[d] for d in D if DMAX[d] < 3.0 and v[d] == v[d]]
    print(f"  {lab:34s} AUC(r3) = {M.auc(pos, neg):.3f}   "
          f"러너날 평균 {np.mean(pos):.3f} / 그외 {np.mean(neg):.3f}")
print("  => 0.692 는 **08:00~08:59 시간외/단일가 구간이 섞인 값**이었다.")
print("     정규장 09:00 하한을 적용한 정확한 값은 0.619 다.")

# ══ (b) 방향성 — 인과적 재측정 ═══════════════════════════════════════
CUM = {}          # 진입 직전 완성봉까지의 당일 하이닉스 누적 %
for t in A:
    k = E.last_done(t["entry_time"])
    b = DAYB.get(t["date"])
    val = np.nan
    if b is not None and k >= 0:
        seg = b[(b["datetime"] <= B3["datetime"].iloc[k])
                & (b["datetime"].dt.strftime("%H%M") >= "0900")]
        if len(seg) >= 2:
            o = float(seg["open"].iloc[0])
            val = (float(seg["close"].iloc[-1]) - o) / o * 100.0
    CUM[(t["date"], t["entry_time"])] = val

AM10 = {d: (lambda x: x)(None) for d in D}
for d in D:
    b = DAYB.get(d)
    if b is None:
        AM10[d] = np.nan; continue
    hh = b["datetime"].dt.strftime("%H%M")
    m = b[(hh >= "0900") & (hh <= "1000")]
    AM10[d] = ((float(m["close"].iloc[-1]) - float(m["open"].iloc[0]))
               / float(m["open"].iloc[0]) * 100.0) if len(m) >= 2 else np.nan


def aligned(t, mode, thr):
    """(정렬여부, 적용가능) — 정렬 = 오전 방향과 진입 방향이 같다."""
    if mode == "cut10":
        if t["entry_time"][11:16] < "10:00":
            return None, False
        v = AM10[t["date"]]
    else:
        v = CUM[(t["date"], t["entry_time"])]
    if v != v or abs(v) < thr:
        return None, False
    up = t["direction"] == "UP_RED"
    return (v > 0) == up, True


def table(mode, thr, W, tag):
    SW = set(W)
    buck = {"순방향": [], "역방향": [], "약함/미적용": []}
    for t in A:
        if t["date"] not in SW:
            continue
        al, ok = aligned(t, mode, thr)
        buck["약함/미적용" if not ok else ("순방향" if al else "역방향")].append(t)
    print(f"\n  ── {tag} ──")
    print(f"    {'구분':12s} {'거래':>4s} {'평균net%':>8s} {'승률':>6s} {'평균MFE':>8s} "
          f"{'>=3%':>6s} {'>=8%':>6s} {'실현P/L':>12s}")
    for k2, g in buck.items():
        if not g:
            print(f"    {k2:12s} (없음)"); continue
        print(f"    {k2:12s} {len(g):4d} {np.mean([t['net_pct'] for t in g]):+8.3f} "
              f"{np.mean([t['net_pct']>0 for t in g])*100:5.1f}% "
              f"{np.mean([t['mfe'] for t in g]):8.2f} "
              f"{np.mean([t['mfe']>=3 for t in g])*100:5.1f}% "
              f"{np.mean([t['mfe']>=8 for t in g])*100:5.1f}% "
              f"{sum(t['pnl_krw'] for t in g):12,.0f}")
    return buck


print("\n" + "=" * 112)
print("(b) 방향성 결합 — 미래정보 제거 후")
print("=" * 112)
THR = float(np.nanmedian([abs(v) for v in CUM.values()]))
print(f"  방식2 강/약 경계 = |진입직전 당일누적| 중앙값 {THR:.3f}%")
print(f"  10:00 이후 진입 거래는 {sum(1 for t in A if t['entry_time'][11:16] >= '10:00')}"
      f"/{len(A)}건 (방식1 적용 대상)")
for mode, mtag, thr in (("cut10", "방식1 오전확정(10:00) x 10:00 이후 진입분만", 1.884),
                        ("entry", "방식2 진입직전 당일누적 (전 거래 인과)", THR)):
    print(f"\n{'='*100}\n{mtag}\n{'='*100}")
    for W, tag in ((D, "78일"), (W30, "30일 IS"), (OOS, "앞48일 OOS")):
        table(mode, thr, W, tag)
