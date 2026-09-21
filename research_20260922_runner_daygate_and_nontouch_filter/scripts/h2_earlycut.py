# -*- coding: utf-8 -*-
"""[H2] (A) '러너 날' 일단위 인과판별 가능성  (B) +1.5% 미도달 손실거래 줄이기.

(B) 는 두 경로를 본다:
   B1 진입필터   — 진입 시점 feature 로 사전 배제 (g6 확장: 조합 + OOS)
   B2 조기중단   — 진입 후 M분 시점에 net <= floor 면 그 봉 종가에 청산
READ-ONLY. 전부 완성봉/인과 정보만.
"""
from __future__ import annotations
import sys
from collections import defaultdict
import numpy as np, pandas as pd
sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, r"C:\Users\FURSYS\Desktop\AI-GAP 2")
import k1_core as K, t1_path as P
import g6_entry as E
import h1_daygate as H

A, TS, D, W30, OOS = H.A, H.TS, H.D, H.W30, H.OOS
AMAP = H.AMAP
rng = np.random.default_rng(20260922)
EXIT_EC = "EARLY_CUT_EXIT"

# ── 거래별 1분 net 시계열 (완성봉 종가) ─────────────────────────────────
NC = {}
for t in A:
    pth = P.path(t)
    NC[(t["date"], t["entry_time"])] = (
        np.array([P.net_of(t["symbol"], t["entry_price"], float(c)) for c in pth["close"]])
        if not pth.empty else np.array([]))


def _auc(pos, neg):
    pos = [x for x in pos if x == x]; neg = [x for x in neg if x == x]
    if not pos or not neg:
        return float("nan")
    return sum((1.0 if p > n else 0.5 if p == n else 0.0)
               for p in pos for n in neg) / (len(pos) * len(neg))


# ═══ (A) 일단위 '러너 날' 판별 가능성 ═══════════════════════════════════
print("=" * 118)
print("[A] '오늘은 러너 나는 날인가' — 오전 정보만으로 일단위 판별이 되는가")
print("=" * 118)
DAYS = sorted({t["date"] for t in A})
DMAX = defaultdict(float)
for t in A:
    DMAX[t["date"]] = max(DMAX[t["date"]], t["mfe"])
B3 = E.B3
DAYB = H.DAYB
PREV = H.PREV

feat = {}
for d in DAYS:
    b = DAYB.get(d)
    if b is None or len(b) < 20:
        continue
    o = float(b["open"].iloc[0])
    m = b[b["datetime"].dt.strftime("%H%M") <= "1000"]      # 09:00~10:00 완성봉
    pv = np.nan
    if PREV.get(d) in DAYB:
        q = DAYB[PREV[d]]
        po = float(q["open"].iloc[0])
        pv = (float(q["high"].max()) - float(q["low"].min())) / po * 100.0
        gap = (o - float(q["close"].iloc[-1])) / float(q["close"].iloc[-1]) * 100.0
    else:
        gap = np.nan
    feat[d] = {
        "오전range%": (float(m["high"].max()) - float(m["low"].min())) / o * 100.0 if len(m) else np.nan,
        "오전|누적|%": abs(float(m["close"].iloc[-1]) - o) / o * 100.0 if len(m) else np.nan,
        "전일range%": pv, "갭%": abs(gap) if gap == gap else np.nan,
        "오전거래량배": (float(m["volume"].mean()) / float(b["volume"].mean())
                    if len(m) and float(b["volume"].mean()) > 0 else np.nan),
    }
run_d = [d for d in feat if DMAX[d] >= 3.0]
flat_d = [d for d in feat if DMAX[d] < 3.0]
print(f"  러너 날(당일 최대 MFE>=3%) {len(run_d)}일 / 그 외 {len(flat_d)}일 (총 {len(feat)}일)")
print(f"  {'feature':14s} {'러너날 평균':>10s} {'그외 평균':>10s} {'러너날 중앙':>10s} "
      f"{'그외 중앙':>10s} {'AUC':>7s}")
print("  " + "-" * 68)
for k in next(iter(feat.values())):
    rv = [feat[d][k] for d in run_d]
    fv = [feat[d][k] for d in flat_d]
    print(f"  {k:14s} {np.nanmean(rv):10.3f} {np.nanmean(fv):10.3f} "
          f"{np.nanmedian(rv):10.3f} {np.nanmedian(fv):10.3f} {_auc(rv, fv):7.3f}")

# ═══ (B) +1.5% 미도달 거래 ══════════════════════════════════════════════
NEVER = [t for t in A if t["tp"] is None]
print("\n" + "=" * 118)
print("[B] +1.5% 미도달 거래 — 규모")
print("=" * 118)
for W, tag in ((W30, "30일 IS"), (OOS, "앞48일 OOS"), (D, "78일")):
    SW = set(W)
    g = [t for t in NEVER if t["date"] in SW]
    print(f"  {tag:10s} 미도달 {len(g):3d}건 / 전체 {sum(1 for t in A if t['date'] in SW):3d}건  "
          f"평균 net {np.mean([t['net_pct'] for t in g]):+6.2f}%  승률 "
          f"{np.mean([t['net_pct']>0 for t in g])*100:4.1f}%  실현 {sum(t['pnl_krw'] for t in g):+12,.0f} KRW  "
          f"(같은창 도달분 {sum(t['pnl_krw'] for t in A if t['date'] in SW and t['tp']):+12,.0f})")

# ── B1. 진입필터 — 진입시점 feature 의 미도달 판별력 (창별) ─────────────
print("\n" + "=" * 118)
print("[B1] 진입필터 — 진입시점 feature 의 '미도달' 판별력 AUC (0.5=무정보)")
print("=" * 118)
KEYS = list(next(iter(E.FE.values())).keys())
print(f"  {'feature':14s} " + " ".join(f"{t:>12s}" for t in ("30일 IS", "앞48일 OOS", "78일")))
print("  " + "-" * 58)
rows = []
for k in KEYS:
    cells = []
    for W in (W30, OOS, D):
        SW = set(W)
        nv = [E.FE[(t["date"], t["entry_time"])][k] for t in A if t["date"] in SW and t["tp"] is None]
        yv = [E.FE[(t["date"], t["entry_time"])][k] for t in A if t["date"] in SW and t["tp"]]
        cells.append(_auc(nv, yv))
    rows.append((abs(cells[2] - 0.5) if cells[2] == cells[2] else -1, k, cells))
for _, k, c in sorted(rows, reverse=True)[:10]:
    same = "" if (c[0] - 0.5) * (c[1] - 0.5) > 0 else "   <= 창끼리 방향 반대"
    print(f"  {k:14s} " + " ".join(f"{x:12.3f}" for x in c) + same)


# ── B2. 조기중단 ────────────────────────────────────────────────────────
def early(hold_min, floor, *, slip=0.0):
    out = []
    for raw in TS:
        key = (raw["date"], raw["entry_time"])
        t = AMAP[key]
        r = dict(raw); r["cut"] = False; r["mfe"] = t["mfe"]
        r["base_net_pct"] = raw["net_pct"]
        nc = NC[key]
        j = hold_min - 1
        if len(nc) > j + 1 and nc[j] <= floor:
            r["cut"] = True
            r["net_pct"] = float(nc[j]) - slip
            r["exit_reason"] = EXIT_EC
        out.append(r)
    return K.size_chain(out, None)


print("\n" + "=" * 130)
print("[B2] 조기중단 — 진입 M분 시점 net <= floor 면 그 봉 종가 청산")
print("=" * 130)
print(f"  {'M분':>4s} {'floor':>6s} {'절단':>4s} {'개선':>4s} {'악화':>4s} "
      f"{'30d uplift':>12s} {'OOS48':>12s} {'78d':>12s} {'PF78':>6s} "
      f"{'미도달절단':>7s} {'>=3손':>5s} {'>=5손':>5s} {'>=8손':>5s}")
print("  " + "-" * 126)
best = []
for M in (5, 10, 15, 20, 30, 45):
    for fl in (-0.3, 0.0, 0.3, 0.5):
        r = early(M, fl)
        a, b, c = H.ev(r, W30), H.ev(r, OOS), H.ev(r, D)
        cut = [x for x in r if x.get("cut")]
        nd = sum(1 for x in cut if AMAP[(x["date"], x["entry_time"])]["tp"] is None)
        if a["up"] > 0 and b["up"] > 0:
            best.append((M, fl, a["up"], b["up"], c["up"]))
        print(f"  {M:4d} {fl:6.2f} {c['cut']:4d} {c['gain']:4d} {c['loss']:4d} "
              f"{a['up']:+12,.0f} {b['up']:+12,.0f} {c['up']:+12,.0f} {c['pf']:6.3f} "
              f"{nd:7d} {c['h3']:5d} {c['h5']:5d} {c['h8']:5d}")
print(f"\n  30d·OOS48 동시 양수 조합: {len(best)}개  {best}")
