# -*- coding: utf-8 -*-
"""[E section2b] profit-lock 파라미터 전수 스윕 — 어디든 30d/OOS48 동시 양수 칸이 있나."""
from __future__ import annotations
import sys
import numpy as np
sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, r"C:\Users\FURSYS\Desktop\AI-GAP 2")
import k1_core as K
import g7_combo as G

A, TS, W30, OOS, D = G.A, G.TS, G.W30, G.OOS, G.D


def lock_exit2(t, floor, *, confirm=1, rp_mfe=None, rp_trend=False):
    p = G.prof(t)
    if p is None:
        return None
    run, below = -1e9, 0
    for j in range(p["i"], p["n"]):
        run = max(run, p["nc"][j])
        if rp_mfe is not None and run >= rp_mfe:
            return None
        if rp_trend and p["trend"][j]:
            return None
        below = below + 1 if p["nc"][j] <= floor else 0
        if below >= confirm:
            return j
    return None


def run(floor, confirm, rp_mfe, rp_trend):
    out = []
    for t in TS:
        r = dict(t); r["fired"] = False
        j = lock_exit2(t, floor, confirm=confirm, rp_mfe=rp_mfe, rp_trend=rp_trend)
        p = G.prof(t)
        if j is not None and j < p["n"] - 1:
            r.update(fired=True, net_pct=float(p["nc"][j]), exit_reason="PROFIT_LOCK_EXIT")
        out.append(r)
    sz = K.size_chain(out, None)
    return (K.krw_pnl(sz, W30) - K.krw_pnl(A, W30),
            K.krw_pnl(sz, OOS) - K.krw_pnl(A, OOS),
            K.krw_pnl(sz, D) - K.krw_pnl(A, D),
            sum(1 for x in out if x["fired"]),
            sum(1 for x in out if x["fired"] and G.MFE[(x["date"], x["entry_time"])] >= 3))


print("=" * 118)
print("[E section2b] profit-lock 스윕 — 30d / OOS48 / 78d uplift (KRW), 발동수, 발동중 MFE>=3% 건수")
print("=" * 118)
print(f"  {'floor':>5s} {'확인':>3s} {'해제':12s} {'30d':>12s} {'OOS48':>12s} {'78d':>12s} "
      f"{'발동':>4s} {'>=3%':>5s} {'동시양수':>7s}")
print("  " + "-" * 114)
good = []
for floor in (0.0, 0.3, 0.5, 0.8, 1.0, 1.2):
    for confirm in (1, 2):
        for rp_name, rp_mfe, rp_tr in (("없음", None, False), ("MFE>=2%", 2.0, False),
                                       ("MFE>=3%", 3.0, False), ("MFE3|추세", 3.0, True),
                                       ("추세만", None, True)):
            a, b, c, n, h3 = run(floor, confirm, rp_mfe, rp_tr)
            ok = "OK" if (a > 0 and b > 0) else ""
            if ok:
                good.append((floor, confirm, rp_name, a, b, c, n, h3))
            print(f"  {floor:5.1f} {confirm:3d} {rp_name:12s} {a:+12,.0f} {b:+12,.0f} {c:+12,.0f} "
                  f"{n:4d} {h3:5d} {ok:>7s}")
print(f"\n  30d 와 OOS48 이 동시에 양수인 조합: {len(good)}개")
for g in good:
    print("   ", g)
