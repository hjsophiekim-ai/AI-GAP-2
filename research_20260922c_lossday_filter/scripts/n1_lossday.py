# -*- coding: utf-8 -*-
"""[N1] 손실 보는 날에 +1.5% 만 먹고 나오는 필터. READ-ONLY.

section 1 : 손실일 구조 — 얼마나, 어디서 잃는가
section 2 : ORACLE 천장 — '오늘 손실일' 을 완벽히 안다면 얼마나 좋아지나
section 3 : 손실일 예측 feature (일단위, **전체 78 영업일** 기준)
"""
from __future__ import annotations
import sys
from collections import defaultdict
import numpy as np, pandas as pd
sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, r"C:\Users\FURSYS\Desktop\AI-GAP 2")
import k1_core as K, t1_path as P
import g6_entry as E

A, TS, D, W30, OOS = E.A, E.TS, E.D, E.W30, E.OOS
B3 = E.B3
THR = 1.50
EXIT_CUT = "LOSSDAY_TP15_EXIT"
rng = np.random.default_rng(20260922)
DAYB = {d: g for d, g in B3.groupby(B3["datetime"].dt.strftime("%Y%m%d"))}

# ── 거래별 +1.5% 도달 정보 ─────────────────────────────────────────────
for t in A:
    pth = P.path(t)
    i, _ = P.first_touch(t, THR) if not pth.empty else (None, None)
    t["n15"] = (P.net_of(t["symbol"], t["entry_price"], float(pth["close"].iloc[i]))
                if i is not None else None)
    t["touch_time"] = str(pth["datetime"].iloc[i]) if i is not None else None

BY = defaultdict(list)
for t in A:
    BY[t["date"]].append(t)
for d in BY:
    BY[d].sort(key=lambda x: x["entry_time"])
DPL = {d: sum(t["pnl_krw"] for t in g) for d, g in BY.items()}
for d in D:
    DPL.setdefault(d, 0.0)

# 그날 **이 거래 진입 전에 이미 청산된** 거래들의 누적 실현손익 (인과적)
PRIOR_PL, PRIOR_N = {}, {}
for d, g in BY.items():
    for t in g:
        done = [x for x in g if x["exit_time"] <= t["entry_time"]]
        PRIOR_PL[(d, t["entry_time"])] = sum(x["pnl_krw"] for x in done)
        PRIOR_N[(d, t["entry_time"])] = len(done)
PREVD = {d: D[i - 1] for i, d in enumerate(D) if i > 0}


def ev(rows, W):
    SW = set(W)
    g = [r for r in rows if r["date"] in SW]
    cut = [r for r in g if r.get("cut")]
    base = K.krw_pnl(A, W)
    return {"pl": K.krw_pnl(rows, W), "up": K.krw_pnl(rows, W) - base,
            "pf": K.pf(rows, W), "mdd": K.mdd_krw(rows, W), "cut": len(cut),
            "gain": sum(1 for r in cut if r["net_pct"] > r["base_net_pct"] + 1e-9),
            "loss": sum(1 for r in cut if r["net_pct"] < r["base_net_pct"] - 1e-9),
            "h3": sum(1 for r in cut if r["mfe"] >= 3),
            "h5": sum(1 for r in cut if r["mfe"] >= 5),
            "h8": sum(1 for r in cut if r["mfe"] >= 8),
            "t1": K.excl_top_krw(rows, W, 1) - K.excl_top_krw(A, W, 1),
            "t3": K.excl_top_krw(rows, W, 3) - K.excl_top_krw(A, W, 3)}


AMAP = {(t["date"], t["entry_time"]): t for t in A}


def build(cutfn, *, slip=0.0):
    """cutfn(t) True 면 그 거래를 +1.5% 도달봉 종가에 청산."""
    out = []
    for raw in TS:
        key = (raw["date"], raw["entry_time"])
        t = AMAP[key]
        r = dict(raw); r["cut"] = False; r["mfe"] = t["mfe"]
        r["base_net_pct"] = raw["net_pct"]
        if t["n15"] is not None and cutfn(t):
            r["cut"] = True
            r["net_pct"] = t["n15"] - slip
            r["exit_reason"] = EXIT_CUT
        out.append(r)
    return K.size_chain(out, None)


if __name__ == "__main__":
    print("=" * 118)
    print("[N1] 손실일 구조")
    print("=" * 118)
    for W, tag in ((W30, "30일 IS"), (OOS, "앞48일 OOS"), (D, "78일")):
        dd = [d for d in W]
        neg = [d for d in dd if DPL[d] < 0]
        pos = [d for d in dd if DPL[d] > 0]
        zero = [d for d in dd if DPL[d] == 0]
        print(f"  {tag:10s} 손실일 {len(neg):2d} / 수익일 {len(pos):2d} / 무거래·보합 {len(zero):2d}  "
              f"손실합 {sum(DPL[d] for d in neg):+12,.0f}  수익합 {sum(DPL[d] for d in pos):+12,.0f}  "
              f"순 {sum(DPL[d] for d in dd):+12,.0f}")

    NEG = [d for d in D if DPL[d] < 0]
    print(f"\n  손실일 {len(NEG)}일 전수 (78일)")
    print(f"    {'날짜':9s} {'거래':>3s} {'일손익':>11s} {'1.5%도달':>7s} {'최대MFE':>7s} "
          f"{'첫거래손익':>11s} {'첫거래비중':>7s}")
    ft_share = []
    for d in sorted(NEG):
        g = BY[d]
        f0 = g[0]["pnl_krw"]
        sh = f0 / DPL[d] * 100 if DPL[d] else 0
        ft_share.append(sh)
        print(f"    {d:9s} {len(g):3d} {DPL[d]:11,.0f} {sum(1 for t in g if t['n15'] is not None):7d} "
              f"{max(t['mfe'] for t in g):7.2f} {f0:11,.0f} {sh:6.0f}%")
    print(f"\n  손실일 {len(NEG)}일 중 첫 거래가 이미 손실인 날: "
          f"{sum(1 for d in NEG if BY[d][0]['pnl_krw'] < 0)}일 "
          f"({sum(1 for d in NEG if BY[d][0]['pnl_krw'] < 0)/len(NEG)*100:.0f}%)")
    tot = sum(DPL[d] for d in NEG)
    f0s = sum(BY[d][0]["pnl_krw"] for d in NEG)
    print(f"  손실일 총손실 {tot:,.0f} 중 **첫 거래분** {f0s:,.0f} ({f0s/tot*100:.0f}%)")
    nt = [t for d in NEG for t in BY[d]]
    print(f"  손실일 거래 {len(nt)}건 중 +1.5% 도달 "
          f"{sum(1 for t in nt if t['n15'] is not None)}건 "
          f"({sum(1 for t in nt if t['n15'] is not None)/len(nt)*100:.0f}%) "
          f"— **나머지는 1.5% 필터로 손댈 수 없다**")

    # ── 2. ORACLE 천장 ─────────────────────────────────────────────────
    print("\n" + "=" * 128)
    print("[2] ORACLE 천장 — 미래정보 사용, 채택 후보 아님")
    print("=" * 128)
    print(f"  {'예지 방식':30s} {'절단':>4s} {'개선':>4s} {'악화':>4s} {'30d uplift':>12s} "
          f"{'OOS48':>12s} {'78d':>12s} {'PF78':>6s} {'>=3손':>5s} {'>=8손':>5s}")
    print("  " + "-" * 124)
    OR = {
        "O1 당일 손실일 예지":        lambda t: DPL[t["date"]] < 0,
        "O2 당일 손실일 + 이 거래도 손실": lambda t: DPL[t["date"]] < 0 and t["net_pct"] < 0,
        "O3 이 거래 최종 < +1.5% 예지": lambda t: t["net_pct"] < THR,
        "O4 이 거래 MFE < 3% 예지":   lambda t: t["mfe"] < 3.0,
    }
    for nm, fn in OR.items():
        r = build(fn)
        a, b, c = ev(r, W30), ev(r, OOS), ev(r, D)
        print(f"  {nm:30s} {c['cut']:4d} {c['gain']:4d} {c['loss']:4d} {a['up']:+12,.0f} "
              f"{b['up']:+12,.0f} {c['up']:+12,.0f} {c['pf']:6.3f} {c['h3']:5d} {c['h8']:5d}")

    # ── 3. 손실일 예측 feature ─────────────────────────────────────────
    print("\n" + "=" * 112)
    print("[3] '오늘은 손실일' 예측 feature — 전체 78영업일 기준 AUC (높을수록 손실일 지시)")
    print("=" * 112)

    def dayfeat(d):
        b = DAYB.get(d)
        if b is None:
            return None
        hh = b["datetime"].dt.strftime("%H%M")
        m = b[(hh >= "0900") & (hh <= "1000")]
        if len(m) < 4:
            return None
        o = float(m["open"].iloc[0]); c = float(m["close"].iloc[-1])
        r = np.diff(np.log(pd.to_numeric(m["close"], errors="coerce").to_numpy()))
        pv = DPL.get(PREVD.get(d), np.nan)
        return {"오전|누적|%": abs(c - o) / o * 100.0,
                "오전range%": (float(m["high"].max()) - float(m["low"].min())) / o * 100.0,
                "오전 rv": float(np.std(r, ddof=1) * np.sqrt(len(r)) * 100.0) if len(r) > 1 else np.nan,
                "오전 거래량": float(m["volume"].mean()) / float(b["volume"].mean()),
                "전일 손익(-)": -pv if pv == pv else np.nan,
                "당일 플래그수": float(sum(1 for i in b.index if i in E.FLAG)),
                "오전 플래그수": float(sum(1 for i in m.index if i in E.FLAG))}

    F = {d: dayfeat(d) for d in D}
    F = {d: v for d, v in F.items() if v}
    neg = [d for d in F if DPL[d] < 0]
    oth = [d for d in F if DPL[d] >= 0]

    def auc(pos, ne):
        pos = [x for x in pos if x == x]; ne = [x for x in ne if x == x]
        if not pos or not ne:
            return float("nan")
        return sum((1.0 if p > n else 0.5 if p == n else 0.0)
                   for p in pos for n in ne) / (len(pos) * len(ne))

    print(f"  손실일 {len(neg)} vs 그 외 {len(oth)} (총 {len(F)}일)")
    print(f"  {'feature':16s} {'AUC 78일':>9s} {'AUC 앞39':>9s} {'AUC 뒤39':>9s} "
          f"{'손실일 평균':>10s} {'그외 평균':>10s}")
    print("  " + "-" * 70)
    S1, S2 = set(D[:39]), set(D[39:])
    for k in next(iter(F.values())):
        a_all = auc([F[d][k] for d in neg], [F[d][k] for d in oth])
        a1 = auc([F[d][k] for d in neg if d in S1], [F[d][k] for d in oth if d in S1])
        a2 = auc([F[d][k] for d in neg if d in S2], [F[d][k] for d in oth if d in S2])
        flip = "" if (a1 - 0.5) * (a2 - 0.5) > 0 else "   <= 반기끼리 방향 반대"
        print(f"  {k:16s} {a_all:9.3f} {a1:9.3f} {a2:9.3f} "
              f"{np.nanmean([F[d][k] for d in neg]):10.3f} "
              f"{np.nanmean([F[d][k] for d in oth]):10.3f}{flip}")
