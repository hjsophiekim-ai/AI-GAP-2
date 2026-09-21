# -*- coding: utf-8 -*-
"""[H] 러너 나는 날/아닌 날 분기 — "안 가는 날만 +1.5% 에서 전량청산". READ-ONLY.

설계
----
selector(t) == True  -> 러너 날/거래로 본다 => **현행 N1+C1 그대로**
selector(t) == False -> +1.5% 최초 도달봉 **종가**에 전량청산 (미도달이면 무변경)

ORACLE 계열은 미래정보를 쓴다 — **상한(천장) 측정 전용**이고 채택 후보가 아니다.
CAUSAL 계열만 실제 후보다(도달 시점까지의 완성봉만 사용).
"""
from __future__ import annotations
import sys, pickle
from collections import defaultdict
import numpy as np, pandas as pd
sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, r"C:\Users\FURSYS\Desktop\AI-GAP 2")
import k1_core as K, t1_path as P
from app.trading.macd2 import n1_adaptive as NA
from app.trading.macd2.models import Direction
import g6_entry as E                      # B3 / HIST / E20 / E50 / last_done

THR = 1.50
EXIT_CUT = "DAYGATE_TP15_EXIT"
D = E.D
W30, OOS = E.W30, E.OOS
A = E.A                                    # BASE = 현행 N1+C1 (mfe 포함)
TS = E.TS
rng = np.random.default_rng(20260922)

# ── 거래별 도달 정보 + 도달시점 인과 feature ────────────────────────────
DAYB = {}
for d, g in E.B3.groupby(E.B3["datetime"].dt.strftime("%Y%m%d")):
    DAYB[d] = g
PREV = {d: D[i - 1] for i, d in enumerate(D) if i > 0}

INFO = {}
for t in A:
    key = (t["date"], t["entry_time"])
    pth = P.path(t)
    if pth.empty:
        INFO[key] = None
        continue
    i, _ = P.first_touch(t, THR)
    if i is None:
        INFO[key] = None
        continue
    sym, ep = t["symbol"], t["entry_price"]
    ts = pd.Timestamp(pth["datetime"].iloc[i])
    k = E.last_done(ts + pd.Timedelta(minutes=1))
    up = t["direction"] == "UP_RED"
    sgn = 1.0 if up else -1.0
    # 당일 09:00 ~ 도달봉까지 하이닉스 (완성봉만)
    db = DAYB.get(t["date"])
    cum = rng_pct = np.nan
    if db is not None and k >= 0:
        seg = db[db["datetime"] <= E.B3["datetime"].iloc[k]]
        if len(seg) >= 2:
            o = float(seg["open"].iloc[0])
            cum = sgn * (float(seg["close"].iloc[-1]) - o) / o * 100.0
            rng_pct = (float(seg["high"].max()) - float(seg["low"].min())) / o * 100.0
    # 전일 하이닉스 변동폭
    pv = np.nan
    pd_ = PREV.get(t["date"])
    if pd_ and pd_ in DAYB:
        q = DAYB[pd_]
        o = float(q["open"].iloc[0])
        pv = (float(q["high"].max()) - float(q["low"].min())) / o * 100.0
    INFO[key] = {
        "i": i, "n15": P.net_of(sym, ep, float(pth["close"].iloc[i])),
        "touch_min": int((ts - pd.Timestamp(t["entry_time"])).total_seconds() // 60),
        "trend": bool(NA.snapshot(E.B3.iloc[: k + 1],
                                  Direction.UP_RED if up else Direction.DOWN_BLUE).ok)
                 if k >= 4 else False,
        "gapd": sgn * (E.HIST[k] - E.HIST[k - 1]) if k >= 1 else np.nan,
        "ema": sgn * (E.E20[k] - E.E50[k]) / E.CL[k] * 100.0 if k >= 0 else np.nan,
        "cum": cum, "rng": rng_pct, "prev_rng": pv,
    }
for t in A:
    t["tp"] = INFO[(t["date"], t["entry_time"])]

DAY_MAX_MFE = defaultdict(float)
for t in A:
    DAY_MAX_MFE[t["date"]] = max(DAY_MAX_MFE[t["date"]], t["mfe"])
# 그날 **앞선** 거래들의 최대 MFE (인과적)
PRIOR = {}
for d, g in defaultdict(list, {k: [x for x in A if x["date"] == k]
                               for k in {y["date"] for y in A}}).items():
    g = sorted(g, key=lambda x: x["entry_time"])
    run = -1.0
    for x in g:
        PRIOR[(x["date"], x["entry_time"])] = run
        run = max(run, x["mfe"])


AMAP = {(t["date"], t["entry_time"]): t for t in A}


def build(sel, *, slip=0.0):
    """원장(TS)에서 시작해 net_pct/exit_reason 만 바꾸고 **size_chain 재실행**."""
    out = []
    for raw in TS:
        key = (raw["date"], raw["entry_time"])
        t = AMAP[key]
        r = dict(raw)
        r["cut"] = False
        r["mfe"] = t["mfe"]
        r["base_net_pct"] = raw["net_pct"]
        tp = t["tp"]
        if tp is not None and not sel(t):
            r["cut"] = True
            r["net_pct"] = tp["n15"] - slip
            r["exit_reason"] = EXIT_CUT
        out.append(r)
    return K.size_chain(out, None)


def ev(rows, W):
    SW = set(W)
    g = [r for r in rows if r["date"] in SW]
    cut = [r for r in g if r.get("cut")]
    base = K.krw_pnl(A, W)
    return {"pl": K.krw_pnl(rows, W), "up": K.krw_pnl(rows, W) - base,
            "pf": K.pf(rows, W), "mdd": K.mdd_krw(rows, W),
            "cut": len(cut),
            "gain": sum(1 for r in cut if r["net_pct"] > r["base_net_pct"] + 1e-9),
            "loss": sum(1 for r in cut if r["net_pct"] < r["base_net_pct"] - 1e-9),
            "h3": sum(1 for r in cut if r["mfe"] >= 3),
            "h5": sum(1 for r in cut if r["mfe"] >= 5),
            "h8": sum(1 for r in cut if r["mfe"] >= 8),
            "win": sum(1 for r in g if r["pnl_krw"] > 0) / max(1, len(g)) * 100,
            "t1": K.excl_top_krw(rows, W, 1) - K.excl_top_krw(A, W, 1),
            "t3": K.excl_top_krw(rows, W, 3) - K.excl_top_krw(A, W, 3)}


# ── selector 정의 ──────────────────────────────────────────────────────
def _f(t, k, d=np.nan):
    return t["tp"][k] if t["tp"] else d


ORACLE = {
    "O1 거래 MFE>=3% 예지":  lambda t: t["mfe"] >= 3.0,
    "O2 당일 최대MFE>=3% 예지": lambda t: DAY_MAX_MFE[t["date"]] >= 3.0,
    "O3 거래 MFE>=2% 예지":  lambda t: t["mfe"] >= 2.0,
}
CAUSAL = {
    "C0 전부 절단(판별 없음)": lambda t: False,
    "C1 N1 상위추세@도달":    lambda t: bool(_f(t, "trend", False)),
    "C2 도달<=10분(빠름)":    lambda t: _f(t, "touch_min", 99) <= 10,
    "C3 당일 하이닉스 |누적|>=0.7%": lambda t: abs(_f(t, "cum", 0.0)) >= 0.7,
    "C4 전일 변동폭>=3%":     lambda t: _f(t, "prev_rng", 0.0) >= 3.0,
    "C5 앞선거래 MFE>=3%":    lambda t: PRIOR.get((t["date"], t["entry_time"]), -1) >= 3.0,
    "C6 추세 or 빠름(C1|C2)": lambda t: bool(_f(t, "trend", False)) or _f(t, "touch_min", 99) <= 10,
    "C7 추세 and 빠름(C1&C2)": lambda t: bool(_f(t, "trend", False)) and _f(t, "touch_min", 99) <= 10,
}

if __name__ == "__main__":
    tt = [t for t in A if t["tp"]]
    print("=" * 134)
    print("[H] 러너 날 분기 — '안 가는 날만 +1.5% 전량청산'")
    print("=" * 134)
    print(f"  +1.5% 도달 {len(tt)}/{len(A)}건. 그중 MFE>=3% {sum(1 for t in tt if t['mfe']>=3)}건 "
          f"({sum(1 for t in tt if t['mfe']>=3)/len(tt)*100:.1f}%) — "
          f"**도달한 거래의 과반이 3% 이상 간다**")
    for W, tag in ((W30, "30일 IS"), (OOS, "앞48일 OOS"), (D, "78일")):
        print(f"  BASE {tag:10s} {K.krw_pnl(A,W):12,.0f} KRW / PF {K.pf(A,W):.3f} / MDD {K.mdd_krw(A,W):6.2f}%")

    for title, SEL in (("ORACLE (미래정보 — 상한 측정 전용)", ORACLE),
                       ("CAUSAL (도달시점 정보만 — 실제 후보)", CAUSAL)):
        print("\n" + "=" * 134)
        print(title)
        print("=" * 134)
        print(f"  {'selector':26s} {'절단':>4s} {'개선':>4s} {'악화':>4s} "
              f"{'30d uplift':>12s} {'PF':>6s} {'MDD%':>6s} {'승률':>6s} "
              f"{'>=3':>4s} {'>=5':>4s} {'>=8':>4s} {'OOS48':>12s} {'78d':>12s}")
        print("  " + "-" * 130)
        for nm, fn in SEL.items():
            r = build(fn)
            a, b, c = ev(r, W30), ev(r, OOS), ev(r, D)
            print(f"  {nm:26s} {a['cut']:4d} {a['gain']:4d} {a['loss']:4d} {a['up']:+12,.0f} "
                  f"{a['pf']:6.3f} {a['mdd']:6.2f} {a['win']:5.1f}% "
                  f"{c['h3']:4d} {c['h5']:4d} {c['h8']:4d} {b['up']:+12,.0f} {c['up']:+12,.0f}")
