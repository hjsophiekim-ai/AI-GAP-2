# -*- coding: utf-8 -*-
"""[PT] +1.5% 최초 도달 시 부분익절(PT15-X). READ-ONLY.

계약
----
진입집합/진입시각/진입가/사이징 불변. 잔량에 **새 stop/floor 를 추가하지 않는다**
— 잔량은 기존 N1+C1 청산 경로를 그대로 타므로 실현률이 원장 `net_pct` 와 같다.

정확성 근거 (해석적 계산이 엔진 재실행과 동일한 이유)
  * 비용이 전부 수량비례이고 최소수수료 0, ETF 세금 0 이라 `net_pct` 는 수량무관.
  * TP/SL/트레일/ETP/H50/C1 판정은 **가격** 기준이라 수량에 의존하지 않는다.
    따라서 90% 잔량의 청산 시각·가격·사유가 100% 일 때와 동일하다.
  * => P/L = tp_qty x entry x net_TP15/100 + rem_qty x entry x net_base/100
  * 사이징 되먹임 경로(그날 첫거래 STOP_LOSS -> x1.20)는 **최종 leg 의
    exit_reason** 이 결정하는데 부분익절은 그것을 바꾸지 않는다.
  * 청산자금 당일 재사용 없음(size_chain cash_reuse=False).

체결 모델: 도달 봉(intrabar high 로 +1.5% 터치)의 **종가**. 낙관적 터치가 아님.
"""
from __future__ import annotations
import sys, pickle
import numpy as np
sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, r"C:\Users\FURSYS\Desktop\AI-GAP 2")
import k1_core as K, t1_path as P

THR = 1.50
CTX = pickle.load(open("_ctx_B.pkl", "rb"))
D = CTX["dates"]
W30, OOS = D[-30:], D[:48]
TS = K.load()
A = K.size_chain(TS, None)                       # BASE = 현행 N1+C1 사이징
rng = np.random.default_rng(20260921)

# ── 도달 정보 (도달봉 종가 net, 전구간 MFE) ────────────────────────────
INFO = {}
for t in A:
    pth = P.path(t)
    key = (t["date"], t["entry_time"])
    if pth.empty:
        INFO[key] = (None, 0.0); continue
    sym, ep = t["symbol"], t["entry_price"]
    mfe = max(P.net_of(sym, ep, float(h)) for h in pth["high"])
    i, _ = P.first_touch(t, THR)
    n15 = P.net_of(sym, ep, float(pth["close"].iloc[i])) if i is not None else None
    INFO[key] = (n15, mfe)
for t in A:
    t["n15"], t["mfe"] = INFO[(t["date"], t["entry_time"])]


def variant(frac, *, slip=0.0, rounding="round"):
    """frac = 도달 시 익절 비율. 반환: 거래별 회계 rows."""
    rows = []
    for t in A:
        r = dict(t)
        q = int(t["qty"])
        r["tp_qty"] = r["rem_qty"] = 0
        r["fired"] = False
        if frac > 0 and t["n15"] is not None and q > 0:
            tq = (int(round(q * frac)) if rounding == "round"
                  else int(q * frac))
            tq = max(0, min(q, tq))
            if tq > 0:
                r["fired"] = True
                r["tp_qty"], r["rem_qty"] = tq, q - tq
        if not r["fired"]:
            r["pnl_krw"] = q * t["entry_price"] * t["net_pct"] / 100.0
            r["eff_net"] = t["net_pct"]
            rows.append(r); continue
        n15 = t["n15"] - slip
        pnl = (r["tp_qty"] * t["entry_price"] * n15 / 100.0
               + r["rem_qty"] * t["entry_price"] * t["net_pct"] / 100.0)
        r["pnl_krw"] = pnl
        r["eff_net"] = pnl / (q * t["entry_price"]) * 100.0 if q else 0.0
        r["net_pct"] = r["eff_net"]           # PF/MDD/승률 계산용
        rows.append(r)
    return rows


def stats(rows, W):
    SW = set(W)
    g = [r for r in rows if r["date"] in SW]
    pl = K.krw_pnl(rows, W)
    wins = sum(1 for r in g if r["pnl_krw"] > 0)
    return {"pl": pl, "pf": K.pf(rows, W), "mdd": K.mdd_krw(rows, W),
            "n": len(g), "win": wins / max(1, len(g)) * 100,
            "avg": pl / max(1, len(g)),
            "t1": K.excl_top_krw(rows, W, 1), "t3": K.excl_top_krw(rows, W, 3),
            "t5": K.excl_top_krw(rows, W, 5)}


FRACS = [("A  BASE", 0.00), ("B  PT15-10", 0.10), ("C  PT15-20", 0.20),
         ("D  PT15-25", 0.25), ("E  PT15-30", 0.30), ("F  PT15-40", 0.40)]
RUNS = {nm: variant(f) for nm, f in FRACS}
BASE = RUNS["A  BASE"]

if __name__ == "__main__":
    touched = [t for t in A if t["n15"] is not None]
    print("=" * 132)
    print("[PT] +1.5% 최초 도달 시 부분익절 — 잔량은 N1+C1 청산 100% 그대로")
    print("=" * 132)
    print(f"  전체 {len(A)}거래 중 +1.5% 도달 {len(touched)}건 ({len(touched)/len(A)*100:.1f}%)")
    for W, tag in ((W30, "30일 IS"), (OOS, "앞48일 OOS"), (D, "78일")):
        SW = set(W)
        n = sum(1 for t in A if t["date"] in SW)
        tc = sum(1 for t in touched if t["date"] in SW)
        print(f"    {tag:11s} 거래 {n:3d} / 도달 {tc:3d} / MFE>=3% "
              f"{sum(1 for t in A if t['date'] in SW and t['mfe']>=3):3d} / >=5% "
              f"{sum(1 for t in A if t['date'] in SW and t['mfe']>=5):2d} / >=8% "
              f"{sum(1 for t in A if t['date'] in SW and t['mfe']>=8):2d}")

    for W, tag in ((W30, "30일 IS"), (OOS, "앞48일 OOS"), (D, "78일 전체")):
        b = stats(BASE, W)
        print(f"\n── {tag} ──  BASE {b['pl']:,.0f} KRW / PF {b['pf']:.3f} / "
              f"MDD {b['mdd']:.2f}% / 승률 {b['win']:.1f}% / 평균 {b['avg']:,.0f}")
        print(f"  {'전략':12s} {'P/L':>12s} {'uplift':>11s} {'PF':>6s} {'MDD%':>7s} "
              f"{'승률':>6s} {'평균/거래':>10s} {'-Top1Δ':>10s} {'-Top3Δ':>10s} {'-Top5Δ':>10s}")
        print("  " + "-" * 116)
        for nm, _f in FRACS:
            s = stats(RUNS[nm], W)
            print(f"  {nm:12s} {s['pl']:12,.0f} {s['pl']-b['pl']:+11,.0f} {s['pf']:6.3f} "
                  f"{s['mdd']:7.2f} {s['win']:5.1f}% {s['avg']:10,.0f} "
                  f"{s['t1']-b['t1']:+10,.0f} {s['t3']-b['t3']:+10,.0f} {s['t5']-b['t5']:+10,.0f}")
