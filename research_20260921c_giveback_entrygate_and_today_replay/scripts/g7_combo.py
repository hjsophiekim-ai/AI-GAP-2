# -*- coding: utf-8 -*-
"""[E section2-3] PROFIT-LOCK + ENTRY GATE 조합. READ-ONLY.

profit-lock = **절대 floor**. MFE(high) 가 +1.5% 에 최초 도달하면 ARM 하고,
그 뒤 완성 1분봉 **종가**가 floor 이하로 내려오면 그 종가에 전량청산.
runner-preserving = 종가기준 최대수익 >= 3.0% 또는 N1 strong trend 확인 시
                    lock 을 **영구 해제**하고 기존 N1+C1 청산으로 복귀.

entry gate = 해당 거래를 **없던 것으로** 제거(removal-only).
  주의: 슬롯이 비어 생겼을 대체 진입은 재현하지 않는다(엔진 재실행 불가).
  w1a 의 CHOP/post-stop 배수와 일예산 30M 재배분은 size_chain 이 순차
  처리하므로 자동 반영된다.
"""
from __future__ import annotations
import sys, pickle
import numpy as np, pandas as pd
sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, r"C:\Users\FURSYS\Desktop\AI-GAP 2")
import k1_core as K, t1_path as P
from app.trading.macd2 import n1_adaptive as NA
from app.trading.macd2.models import Direction
import g6_entry as E

D, W30, OOS = E.D, E.W30, E.OOS
TS, A = E.TS, E.A
AMAP = {(t["date"], t["entry_time"]): t for t in A}
MFE = {k: t["mfe"] for k, t in AMAP.items()}
EXIT_LOCK = "PROFIT_LOCK_EXIT"
rng = np.random.default_rng(20260921)
_PR = {}


def prof(t):
    k = (t["date"], t["entry_time"])
    if k in _PR:
        return _PR[k]
    i, _ = P.first_touch(t, 1.50)
    if i is None:
        _PR[k] = None
        return None
    p = P.path(t)
    nc = np.array([P.net_of(t["symbol"], t["entry_price"], float(c)) for c in p["close"]])
    up = t["direction"] == "UP_RED"
    dirn = Direction.UP_RED if up else Direction.DOWN_BLUE
    trend, seen = [], {}
    for d in p["datetime"]:
        kk = E.last_done(pd.Timestamp(d) + pd.Timedelta(minutes=1))
        if kk not in seen:
            seen[kk] = bool(NA.snapshot(E.B3.iloc[: kk + 1], dirn).ok) if kk >= 4 else False
        trend.append(seen[kk])
    _PR[k] = {"i": i, "nc": nc, "trend": trend, "n": len(p)}
    return _PR[k]


def lock_exit(t, floor, *, rp_mfe3=False, rp_trend=False, delay=0):
    """profit-lock 발동 봉 index (없으면 None)."""
    p = prof(t)
    if p is None:
        return None
    run = -1e9
    for j in range(p["i"], p["n"]):
        run = max(run, p["nc"][j])
        if rp_mfe3 and run >= 3.0:
            return None                      # 영구 해제 -> 기존 N1+C1 복귀
        if rp_trend and p["trend"][j]:
            return None
        if p["nc"][j] <= floor:
            return min(j + delay, p["n"] - 1)
    return None


def _f(t, k):
    return E.FE[(t["date"], t["entry_time"])][k]


def _num(x):
    return x == x


GATES = {
    "G1 gapD<=0 (모멘텀 미확장)": lambda t: _num(_f(t, "gapΔ")) and _f(t, "gapΔ") <= 0,
    "G2 CHOP & 비추세": lambda t: _f(t, "entry_chop") == 1 and _f(t, "N1추세ok") == 0,
    "G3 비추세(N1 off-trend)": lambda t: _f(t, "N1추세ok") == 0,
    "G1b gapD<143.8 (임계적합)": lambda t: _num(_f(t, "gapΔ")) and _f(t, "gapΔ") < 143.8,
}


def build(gate=None, floor=None, *, rp_mfe3=False, rp_trend=False, delay=0, slip=0.0):
    out = []
    for t in TS:
        if gate is not None and gate(t):
            continue
        r = dict(t)
        r["fired"] = False
        r["base_net_pct"] = t["net_pct"]
        r["base_exit_reason"] = t["exit_reason"]
        if floor is not None:
            j = lock_exit(t, floor, rp_mfe3=rp_mfe3, rp_trend=rp_trend, delay=delay)
            p = prof(t)
            if j is not None and j < p["n"] - 1:
                r.update(fired=True, net_pct=float(p["nc"][j] - slip), exit_reason=EXIT_LOCK)
        out.append(r)
    return out


def ev(sim, W):
    sz = K.size_chain(sim, None)
    SW = set(W)
    kept = [x for x in sim if x["date"] in SW]
    fired = [x for x in kept if x["fired"]]
    keys = {(x["date"], x["entry_time"]) for x in sim}
    dropped = [t for t in A if t["date"] in SW and (t["date"], t["entry_time"]) not in keys]
    base = K.krw_pnl(A, W)
    return {"sz": sz, "krw": K.krw_pnl(sz, W), "up": K.krw_pnl(sz, W) - base,
            "pf": K.pf(sz, W), "mdd": K.mdd_krw(sz, W), "n": len(kept),
            "fired": len(fired),
            "gain": sum(1 for x in fired if x["net_pct"] > x["base_net_pct"] + 1e-9),
            "loss": sum(1 for x in fired if x["net_pct"] < x["base_net_pct"] - 1e-9),
            "dropB": sum(1 for t in dropped if t["mfe"] < 1.5),
            "dropA": sum(1 for t in dropped if t["mfe"] >= 1.5),
            "d3": sum(1 for t in dropped if t["mfe"] >= 3),
            "d5": sum(1 for t in dropped if t["mfe"] >= 5),
            "d8": sum(1 for t in dropped if t["mfe"] >= 8),
            "h3": sum(1 for x in fired if MFE[(x["date"], x["entry_time"])] >= 3),
            "h5": sum(1 for x in fired if MFE[(x["date"], x["entry_time"])] >= 5),
            "h8": sum(1 for x in fired if MFE[(x["date"], x["entry_time"])] >= 8),
            "t1": K.excl_top_krw(sz, W, 1) - K.excl_top_krw(A, W, 1),
            "t3": K.excl_top_krw(sz, W, 3) - K.excl_top_krw(A, W, 3)}


def boot(sim, W, n=10000):
    da, db = K.daily_krw(A, W), K.daily_krw(K.size_chain(sim, None), W)
    v = np.array([db[d] - da[d] for d in W])
    bt = v[rng.integers(0, len(v), size=(n, len(v)))].sum(axis=1)
    return (bt > 0).mean() * 100, np.percentile(bt, 5), np.percentile(bt, 50)


LOCKS = [("P0 기존 N1+C1", None, False, False)]
for _nm, _fl in (("P1 floor +0.5%", 0.5), ("P2 floor +0.8%", 0.8), ("P3 floor +1.0%", 1.0)):
    LOCKS.append((_nm, _fl, False, False))
    LOCKS.append((_nm + " +RP(MFE3)", _fl, True, False))
    LOCKS.append((_nm + " +RP(MFE3|추세)", _fl, True, True))


if __name__ == "__main__":
    for W, tag in ((W30, "30일"), (OOS, "앞48일 OOS"), (D, "78일")):
        print(f"  BASE {tag:10s} {K.krw_pnl(A,W):12,.0f} KRW  PF {K.pf(A,W):.3f}  "
              f"MDD {K.mdd_krw(A,W):6.2f}  거래 {sum(1 for t in A if t['date'] in set(W))}")
    print()
    print("=" * 136)
    print("[E section2] PROFIT-LOCK 단독 — floor 는 절대 수익 하한")
    print("=" * 136)
    print(f"  {'후보':32s} {'발동':>4s} {'개선':>4s} {'악화':>4s} {'30d uplift':>12s} "
          f"{'PF':>6s} {'MDD%':>6s} {'발동중>=3%':>8s} {'>=5%':>5s} {'>=8%':>5s} "
          f"{'OOS48':>12s} {'78d':>12s}")
    print("  " + "-" * 132)
    for nm, fl, rm, rt in LOCKS:
        sim = build(None, fl, rp_mfe3=rm, rp_trend=rt)
        a, b, c = ev(sim, W30), ev(sim, OOS), ev(sim, D)
        print(f"  {nm:32s} {a['fired']:4d} {a['gain']:4d} {a['loss']:4d} {a['up']:+12,.0f} "
              f"{a['pf']:6.3f} {a['mdd']:6.2f} {a['h3']:8d} {a['h5']:5d} {a['h8']:5d} "
              f"{b['up']:+12,.0f} {c['up']:+12,.0f}")

    print()
    print("=" * 128)
    print("[E section1b] ENTRY GATE 단독 (removal-only)")
    print("=" * 128)
    print(f"  {'게이트':28s} {'제외':>4s} {'B제거':>5s} {'A제거':>5s} {'>=3':>4s} {'>=5':>4s} {'>=8':>4s} "
          f"{'30d uplift':>12s} {'PF':>6s} {'MDD%':>6s} {'OOS48':>12s} {'78d':>12s}")
    print("  " + "-" * 124)
    for nm, fn in GATES.items():
        sim = build(fn, None)
        a, b, c = ev(sim, W30), ev(sim, OOS), ev(sim, D)
        print(f"  {nm:28s} {a['dropA']+a['dropB']:4d} {a['dropB']:5d} {a['dropA']:5d} "
              f"{a['d3']:4d} {a['d5']:4d} {a['d8']:4d} {a['up']:+12,.0f} {a['pf']:6.3f} "
              f"{a['mdd']:6.2f} {b['up']:+12,.0f} {c['up']:+12,.0f}")
