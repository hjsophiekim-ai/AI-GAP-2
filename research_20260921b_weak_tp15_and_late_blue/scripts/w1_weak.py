# -*- coding: utf-8 -*-
"""[연구 1] 약한 장 +1.5% 조기익절 — 최근 30거래일. READ-ONLY.

약한 장 후보는 **진입 시각 이전에 알 수 있는 production/저장 데이터만** 쓴다.
지수/breadth 는 저장본이 2026-07-10 스냅샷 1건뿐이라(advancers/decliners=None)
사용 불가 — 하이닉스(감시종목)와 production 판정값으로 대체한다.
"""
from __future__ import annotations
import sys, pickle, statistics as st
import numpy as np, pandas as pd
sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, r"C:\Users\FURSYS\Desktop\AI-GAP 2")
import k1_core as K, t1_path as P, t2_regime as RG, t3_tp15 as T

CTX = pickle.load(open("_ctx_B.pkl", "rb"))
D = CTX["dates"]; W30 = D[-30:]
TS = K.load()
A = K.size_chain(TS, None)
A30 = K.krw_pnl(A, W30)
RUN8 = {(t["date"], t["entry_time"]) for t in A if t["peak_net_pct"] >= 8.0}
BARS = CTX["hynix_bars_3m"]
rng = np.random.default_rng(20260921)


# ── 후보 feature (전부 진입 이전 정보) ──────────────────────────────────
def _day_bars_before(trade):
    """그날 09:00 ~ 진입 직전 완성봉까지의 하이닉스 3분봉."""
    t = pd.Timestamp(trade["entry_time"])
    day0 = t.normalize()
    m = ((BARS["datetime"] >= day0) &
         (BARS["datetime"] + pd.Timedelta(minutes=3) <= t))
    return BARS.loc[m]


_FIRST_MFE: dict = {}
for _t in A:
    d = _t["date"]
    if d not in _FIRST_MFE:
        _FIRST_MFE[d] = (_t["entry_time"], _t["peak_net_pct"])


def W1(trade) -> bool:
    """W1  N1 비추세 + CHOP — production 판정 두 개의 교집합(새 임계값 0개).
    78일 연구에서 유일하게 양수였던 조건이다."""
    return RG.R1(trade) and RG.R2(trade)


def W2(trade) -> bool:
    """W2  그날 **앞선 거래**가 약했다 — 첫 거래 MFE < 1.5%.
    한 번에 한 포지션이므로 첫 거래는 이 진입 전에 이미 닫혀 있다(미래정보 아님).
    첫 거래 자신에게는 적용되지 않는다."""
    ft, mfe = _FIRST_MFE.get(trade["date"], (None, None))
    if ft is None or ft >= trade["entry_time"]:
        return False
    return float(mfe) < 1.5


def W3(trade) -> bool:
    """W3  장 초반 기초자산 방향성 약함 — 09:00~진입 직전 하이닉스
    |누적변화율| < 0.5% 그리고 고저 range < 1.0%."""
    b = _day_bars_before(trade)
    if len(b) < 2:
        return False
    o = float(b["open"].iloc[0]); c = float(b["close"].iloc[-1])
    hi = float(b["high"].max()); lo = float(b["low"].min())
    if o <= 0:
        return False
    return abs(c - o) / o * 100.0 < 0.5 and (hi - lo) / o * 100.0 < 1.0


CANDS = {"W1 비추세+CHOP": W1, "W2 첫거래MFE<1.5": W2, "W3 장초반 무방향": W3,
         "(대조) 전거래": lambda t: True}


def run(fn, thr=1.50, **kw):
    sim = T.simulate(TS, fn, thr, **kw)
    return sim, K.size_chain(sim, None)


print("=" * 142)
print(f"[연구 1] 약한 장 +1.5% 조기익절 — 30거래일 {W30[0]}~{W30[-1]}")
print("=" * 142)
print(f"  BASE: {A30:,.0f} KRW / PF {K.pf(A, W30):.3f} / MDD {K.mdd_krw(A, W30):.2f}% / "
      f"거래 {sum(1 for t in A if t['date'] in set(W30))}건 / runner 2건")
print()
h = (f"{'후보':18s} {'weak일':>6s} {'weak거래':>7s} {'발동':>4s} {'30d P/L':>12s} {'uplift':>11s} "
     f"{'PF':>6s} {'MDD%':>6s} {'개선':>4s} {'악화':>4s} {'runner손상':>9s} "
     f"{'-Top1':>11s} {'-Top3':>11s}")
print(h); print("-" * 142)
RES = {}
for name, fn in CANDS.items():
    sim, r = run(fn, 1.50)
    s30 = set(W30)
    fired = [x for x in sim if x["tp15_fired"] and x["date"] in s30]
    wk = [x for x in sim if x["regime_weak"] and x["date"] in s30]
    up = K.krw_pnl(r, W30) - A30
    gain = sum(1 for x in fired if x["net_pct"] > x["base_net_pct"])
    loss = sum(1 for x in fired if x["net_pct"] < x["base_net_pct"])
    rd = sum(1 for x in fired if (x["date"], x["entry_time"]) in RUN8)
    RES[name] = (sim, r, up, fired)
    print(f"{name:18s} {len({x['date'] for x in wk}):6d} {len(wk):7d} {len(fired):4d} "
          f"{K.krw_pnl(r, W30):12,.0f} {up:+11,.0f} {K.pf(r, W30):6.3f} {K.mdd_krw(r, W30):6.2f} "
          f"{gain:4d} {loss:4d} {rd:9d} "
          f"{K.excl_top_krw(r, W30, 1)-K.excl_top_krw(A, W30, 1):+11,.0f} "
          f"{K.excl_top_krw(r, W30, 3)-K.excl_top_krw(A, W30, 3):+11,.0f}")

print()
print("=" * 90)
print("threshold 민감도 (30d uplift KRW)")
print("=" * 90)
print(f"{'후보':18s} " + " ".join(f"{t:>14s}" for t in ("+1.25%", "+1.50%", "+1.75%")))
print("-" * 90)
for name, fn in CANDS.items():
    vals = [K.krw_pnl(run(fn, t)[1], W30) - A30 for t in (1.25, 1.50, 1.75)]
    print(f"{name:18s} " + " ".join(f"{v:+14,.0f}" for v in vals))

print()
print("=" * 90)
print("부트스트랩 10,000회 (일단위, 30일)")
print("=" * 90)
print(f"{'후보':18s} {'P(>0)':>8s} {'p5':>12s} {'중앙':>12s} {'p95':>12s}")
print("-" * 90)
for name, (_, r, up, _f) in RES.items():
    da, db = K.daily_krw(A, W30), K.daily_krw(r, W30)
    v = np.array([db[d] - da[d] for d in W30])
    bt = v[rng.integers(0, len(v), size=(10000, len(v)))].sum(axis=1)
    print(f"{name:18s} {(bt>0).mean()*100:7.2f}% {np.percentile(bt,5):+12,.0f} "
          f"{np.percentile(bt,50):+12,.0f} {np.percentile(bt,95):+12,.0f}")

print()
print("=" * 110)
print("발동 거래 전량 (각 후보)")
print("=" * 110)
AMAP = {(a["date"], a["entry_time"]): a for a in A}
for name, (_, sized, _, fired) in RES.items():
    if name.startswith("(대조)"):
        continue
    SMAP = {(x["date"], x["entry_time"]): x for x in sized}
    print(f"\n[{name}] {len(fired)}건")
    if not fired:
        print("   (없음)"); continue
    print(f"   {'날짜':9s} {'진입':6s} {'sl':>2s} {'세션':4s} {'기존사유':24s} {'기존%':>7s} "
          f"{'TP15%':>7s} {'차이':>7s} {'ΔKRW':>10s}")
    for x in fired:
        print(f"   {x['date']:9s} {x['entry_time'][11:16]:6s} {x['slot']:2d} "
              f"{('오전' if x['session']=='MORNING' else '오후'):4s} {x['base_exit_reason']:24s} "
              f"{x['base_net_pct']:7.2f} {x['net_pct']:7.2f} "
              f"{x['net_pct']-x['base_net_pct']:+7.2f} "
              f"{SMAP[(x['date'],x['entry_time'])]['pnl_krw'] - AMAP[(x['date'],x['entry_time'])]['pnl_krw']:+10,.0f}")
