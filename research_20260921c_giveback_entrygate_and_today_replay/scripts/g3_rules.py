# -*- coding: utf-8 -*-
"""[G section3-4] +1.5% 도달 후 '반납 경고' 규칙 3종 + 평가. READ-ONLY.

청산 방식 = 도달 즉시 전량매도가 **아니라**, 도달 시점에 가드를 arm 하고
약해질 때만 판다. 판정은 전부 **완성 1분봉 종가 / 완성 3분봉**만 쓴다.
"""
from __future__ import annotations
import sys, pickle
import numpy as np, pandas as pd
sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, r"C:\Users\FURSYS\Desktop\AI-GAP 2")
import k1_core as K, t1_path as P
import g2_feat as F

THR = 1.50
EXIT_TAG = "MFE15_GUARD_EXIT"
CTX = pickle.load(open("_ctx_B.pkl", "rb"))
D = CTX["dates"]; W30 = D[-30:]; S30 = set(W30)
TS = K.load()
A = K.size_chain(TS, None)
AMAP = {(t["date"], t["entry_time"]): t for t in A}
RUN8 = {k for k, t in AMAP.items() if t["peak_net_pct"] >= 8.0}
rng = np.random.default_rng(20260921)
_CACHE = {}


def prof(t):
    """도달 이후 판정에 필요한 1분봉 net 시계열 (전부 완성봉 종가)."""
    key = (t["date"], t["entry_time"])
    if key in _CACHE:
        return _CACHE[key]
    i, _ = P.first_touch(t, THR)
    if i is None:
        _CACHE[key] = None; return None
    pth = P.path(t)
    sym, ep = t["symbol"], t["entry_price"]
    nc = np.array([P.net_of(sym, ep, float(c)) for c in pth["close"]])
    o = pth["open"].to_numpy(float); h = pth["high"].to_numpy(float)
    l = pth["low"].to_numpy(float); c = pth["close"].to_numpy(float)
    pos = (c - l) / np.maximum(h - l, 1e-9)
    # 도달 이후 각 1분봉 시점에 '완성되어 있는' 하이닉스 3분봉 인덱스
    kidx = [F.last_done_idx(pd.Timestamp(d) + pd.Timedelta(minutes=1))
            for d in pth["datetime"]]
    out = {"i": i, "nc": nc, "pos": pos, "k": kidx, "n": len(pth)}
    _CACHE[key] = out
    return out


# ── 규칙 3종 : 발동 봉 인덱스 반환 (없으면 None) ─────────────────────────
def R1(t, p, *, give=1.0):
    """R1 트레일 floor — 도달 후 고점(종가기준) 대비 give %p 반납하면 청산.
    예측 없음. 반납 자체를 상한으로 막는다."""
    run = -1e9
    for j in range(p["i"], p["n"]):
        run = max(run, p["nc"][j])
        if p["nc"][j] <= run - give:
            return j
    return None


def R2(t, p, *, pos_thr=0.35):
    """R2 도달봉 되밀림 — +1.5% 에 닿은 그 1분봉이 종가위치 < pos_thr 로
    (긴 위꼬리) 끝나면 다음 봉 종가에 청산. SEVERE 판별 AUC 0.95/0.13."""
    if p["pos"][p["i"]] < pos_thr:
        return min(p["i"] + 1, p["n"] - 1)
    return None


def R3(t, p):
    """R3 모멘텀 소진 — 도달 후 하이닉스 3분 **완성봉**에서 보유방향 gap 이
    2연속 축소되면 그 봉이 닫힌 뒤 첫 1분봉 종가에 청산."""
    sgn = 1.0 if t["direction"] == "UP_RED" else -1.0
    seen = set()
    for j in range(p["i"], p["n"]):
        k = p["k"][j]
        if k < 2 or k in seen:
            continue
        seen.add(k)
        g0, g1, g2 = sgn * F.HIST[k], sgn * F.HIST[k - 1], sgn * F.HIST[k - 2]
        if g0 < g1 < g2:
            return j
    return None


def R12(t, p):
    a, b = R1(t, p), R2(t, p)
    return min(x for x in (a, b) if x is not None) if (a is not None or b is not None) else None


def R13(t, p):
    a, b = R1(t, p), R3(t, p)
    return min(x for x in (a, b) if x is not None) if (a is not None or b is not None) else None


def R23(t, p):
    a, b = R2(t, p), R3(t, p)
    return min(x for x in (a, b) if x is not None) if (a is not None or b is not None) else None


RULES = {"R1 트레일 -1.0%p": R1, "R2 도달봉 되밀림": R2, "R3 gap 2연속축소": R3,
         "R1+R2": R12, "R1+R3": R13, "R2+R3": R23}


def simulate(rule, *, delay=0, slip=0.0, thr=THR):
    out = []
    for t in TS:
        r = dict(t); r["fired"] = False
        r["base_net_pct"] = t["net_pct"]; r["base_exit_reason"] = t["exit_reason"]
        p = prof(t)
        if p is not None:
            j = rule(t, p)
            if j is not None:
                j = min(j + delay, p["n"] - 1)
                new = p["nc"][j] - slip
                if j < p["n"] - 1:      # 마지막 봉이면 기존 청산과 동일
                    r.update(fired=True, net_pct=float(new), exit_reason=EXIT_TAG)
        out.append(r)
    return out


def metrics(rule, *, W=W30, **kw):
    sim = simulate(rule, **kw)
    sz = K.size_chain(sim, None)
    SW = set(W)
    fired = [x for x in sim if x["fired"] and x["date"] in SW]
    gain = sum(1 for x in fired if x["net_pct"] > x["base_net_pct"] + 1e-9)
    loss = sum(1 for x in fired if x["net_pct"] < x["base_net_pct"] - 1e-9)
    base = K.krw_pnl(A, W)
    return {"sim": sim, "sz": sz, "n": len(fired), "gain": gain, "loss": loss,
            "krw": K.krw_pnl(sz, W), "up": K.krw_pnl(sz, W) - base,
            "pf": K.pf(sz, W), "mdd": K.mdd_krw(sz, W),
            "runner": sum(1 for x in fired if (x["date"], x["entry_time"]) in RUN8),
            "d1": K.excl_top_krw(sz, W, 1) - K.excl_top_krw(A, W, 1),
            "d3": K.excl_top_krw(sz, W, 3) - K.excl_top_krw(A, W, 3),
            "fired": fired}


if __name__ == "__main__":
    print("=" * 140)
    print(f"[G section3-4] 반납 경고 규칙 — 30영업일 {W30[0]}~{W30[-1]}")
    print("=" * 140)
    print(f"  BASE: {K.krw_pnl(A, W30):,.0f} KRW / PF {K.pf(A, W30):.3f} / "
          f"MDD {K.mdd_krw(A, W30):.2f}% / 거래 {sum(1 for t in A if t['date'] in S30)}건 / runner 2건")
    print()
    print(f"  {'규칙':18s} {'발동':>4s} {'개선':>4s} {'악화':>4s} {'30d P/L':>12s} {'uplift':>11s} "
          f"{'PF':>6s} {'MDD%':>6s} {'runner손상':>9s} {'-Top1Δ':>11s} {'-Top3Δ':>11s}")
    print("  " + "-" * 136)
    M = {}
    for name, fn in RULES.items():
        m = metrics(fn); M[name] = m
        print(f"  {name:18s} {m['n']:4d} {m['gain']:4d} {m['loss']:4d} {m['krw']:12,.0f} "
              f"{m['up']:+11,.0f} {m['pf']:6.3f} {m['mdd']:6.2f} {m['runner']:9d} "
              f"{m['d1']:+11,.0f} {m['d3']:+11,.0f}")

    print()
    print("=" * 100)
    print("트레일 폭 민감도 (R1 단독, 30d uplift KRW)")
    print("=" * 100)
    for g in (0.5, 0.75, 1.0, 1.25, 1.5, 2.0):
        m = metrics(lambda t, p, gg=g: R1(t, p, give=gg))
        print(f"  give {g:4.2f}%p  발동 {m['n']:3d}  uplift {m['up']:+12,.0f}  "
              f"PF {m['pf']:6.3f}  MDD {m['mdd']:6.2f}  runner손상 {m['runner']}  "
              f"개선/악화 {m['gain']}/{m['loss']}")
    print()
    print("종가위치 임계 민감도 (R2 단독)")
    for q in (0.25, 0.30, 0.35, 0.40, 0.50):
        m = metrics(lambda t, p, qq=q: R2(t, p, pos_thr=qq))
        print(f"  pos<{q:4.2f}   발동 {m['n']:3d}  uplift {m['up']:+12,.0f}  "
              f"PF {m['pf']:6.3f}  MDD {m['mdd']:6.2f}  runner손상 {m['runner']}  "
              f"개선/악화 {m['gain']}/{m['loss']}")
