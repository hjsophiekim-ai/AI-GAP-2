# -*- coding: utf-8 -*-
"""[E section1] ENTRY QUALITY GATE — MFE<1.5% 거래를 진입시점에 걸러낼 수 있나.

라벨 A: 보유중 MFE(high기준) >= +1.5%   /  B: < +1.5%
feature 는 **진입 시각 이전에 완성된** 하이닉스 3분봉 + 원장 진입정보만.
READ-ONLY, production 무수정, 새 ML 없음.
"""
from __future__ import annotations
import sys, pickle
import numpy as np, pandas as pd
sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, r"C:\Users\FURSYS\Desktop\AI-GAP 2")
import k1_core as K, t1_path as P
from app.trading.macd2 import config as C
from app.trading.macd2 import n1_adaptive as NA
from app.trading.macd2.models import Direction

CTX = pickle.load(open("_ctx_B.pkl", "rb"))
D = CTX["dates"]; W30 = D[-30:]; OOS = D[:48]
S30 = set(W30)
B3 = CTX["hynix_bars_3m"].reset_index(drop=True)
FLAG = CTX["flags_by_idx"]
_c = pd.to_numeric(B3["close"], errors="coerce")
_ef = _c.ewm(span=C.EMA_FAST, adjust=False).mean()
_es = _c.ewm(span=C.EMA_SLOW, adjust=False).mean()
MACD = _ef - _es
HIST = (MACD - MACD.ewm(span=C.EMA_SIGNAL, adjust=False).mean()).to_numpy()
E20 = _c.ewm(span=C.H50_TREND_EMA_FAST, adjust=False).mean().to_numpy()
E50 = _c.ewm(span=C.H50_TREND_EMA_SLOW, adjust=False).mean().to_numpy()
CL = _c.to_numpy()
BT = pd.DatetimeIndex(B3["datetime"])
DAY = BT.normalize()

TS = K.load()
A = K.size_chain(TS, None)


def last_done(ts) -> int:
    return int(BT.searchsorted(pd.Timestamp(ts) - pd.Timedelta(minutes=3), side="right") - 1)


MFE = {}
for t in A:
    p = P.path(t)
    MFE[(t["date"], t["entry_time"])] = (
        max(P.net_of(t["symbol"], t["entry_price"], float(h)) for h in p["high"])
        if not p.empty else 0.0)
for t in A:
    t["mfe"] = MFE[(t["date"], t["entry_time"])]


def entry_feats(t) -> dict:
    k = last_done(t["entry_time"])
    up = t["direction"] == "UP_RED"
    sgn = 1.0 if up else -1.0
    f = {"slot": t["slot"], "오전": 1 if t["session"] == "MORNING" else 0,
         "entry_chop": 1 if t["entry_chop"] else 0,
         "진입시각": int(t["entry_time"][11:13]) * 60 + int(t["entry_time"][14:16]),
         "UP_RED": 1 if up else 0}
    if k < 4:
        for kk in ("N1추세ok", "gap", "gapΔ", "gap가속", "ema20_50%", "ema20기울기",
                   "직전봉mom", "3봉mom", "플래그→진입봉", "당일앞선플래그"):
            f[kk] = np.nan
        return f
    f["N1추세ok"] = 1 if NA.snapshot(B3.iloc[: k + 1],
                                    Direction.UP_RED if up else Direction.DOWN_BLUE).ok else 0
    h0, h1, h2 = HIST[k], HIST[k - 1], HIST[k - 2]
    f["gap"] = sgn * h0
    f["gapΔ"] = sgn * (h0 - h1)
    f["gap가속"] = sgn * ((h0 - h1) - (h1 - h2))
    f["ema20_50%"] = sgn * (E20[k] - E50[k]) / CL[k] * 100.0
    f["ema20기울기"] = sgn * (E20[k] - E20[k - 3]) / CL[k] * 100.0
    f["직전봉mom"] = sgn * (CL[k] - CL[k - 1]) / CL[k - 1] * 100.0
    f["3봉mom"] = sgn * (CL[k] - CL[k - 3]) / CL[k - 3] * 100.0
    want = Direction.UP_RED if up else Direction.DOWN_BLUE
    fl = [i for i in range(max(0, k - 20), k + 1) if FLAG.get(i) == want]
    f["플래그→진입봉"] = (k - fl[-1]) if fl else np.nan
    d0 = DAY[k]
    f["당일앞선플래그"] = sum(1 for i, v in FLAG.items() if i <= k and DAY[i] == d0)
    return f


FE = {(t["date"], t["entry_time"]): entry_feats(t) for t in A}
KEYS = list(next(iter(FE.values())).keys())


def _auc(pos, neg):
    pos = [x for x in pos if x == x]; neg = [x for x in neg if x == x]
    if not pos or not neg:
        return float("nan")
    return sum((1.0 if p > n else 0.5 if p == n else 0.0)
               for p in pos for n in neg) / (len(pos) * len(neg))


if __name__ == "__main__":
    G30 = [t for t in A if t["date"] in S30]
    Agrp = [t for t in G30 if t["mfe"] >= 1.5]
    Bgrp = [t for t in G30 if t["mfe"] < 1.5]
    print("=" * 118)
    print(f"[E section1] ENTRY QUALITY GATE — 30영업일 {W30[0]}~{W30[-1]}")
    print("=" * 118)
    print(f"  A (MFE>=1.5%) {len(Agrp)}건  평균net {np.mean([t['net_pct'] for t in Agrp]):+.2f}%  "
          f"실현 {sum(t['pnl_krw'] for t in Agrp):+,.0f} KRW")
    print(f"  B (MFE< 1.5%) {len(Bgrp)}건  평균net {np.mean([t['net_pct'] for t in Bgrp]):+.2f}%  "
          f"실현 {sum(t['pnl_krw'] for t in Bgrp):+,.0f} KRW  승률 "
          f"{np.mean([t['net_pct']>0 for t in Bgrp])*100:.1f}%")
    print(f"  MFE 분포: >=3% {sum(1 for t in G30 if t['mfe']>=3):2d}건 / "
          f">=5% {sum(1 for t in G30 if t['mfe']>=5):2d}건 / >=8% {sum(1 for t in G30 if t['mfe']>=8):2d}건")
    print()
    print(f"  {'feature':14s} {'A평균':>10s} {'B평균':>10s} {'A중앙':>10s} {'B중앙':>10s} "
          f"{'AUC(B↑)':>8s} {'|Δ|':>6s}")
    print("  " + "-" * 74)
    rows = []
    for kk in KEYS:
        av = [FE[(t["date"], t["entry_time"])][kk] for t in Agrp]
        bv = [FE[(t["date"], t["entry_time"])][kk] for t in Bgrp]
        au = _auc(bv, av)
        rows.append((abs(au - 0.5) if au == au else -1, kk, av, bv, au))
    for _, kk, av, bv, au in sorted(rows, reverse=True):
        a = [x for x in av if x == x]; b = [x for x in bv if x == x]
        print(f"  {kk:14s} {np.mean(a):10.3f} {np.mean(b):10.3f} {np.median(a):10.3f} "
              f"{np.median(b):10.3f} {au:8.3f} {abs(au-0.5):6.3f}")

    print("\n  단일 임계 스캔 (30일) — '제외' 규칙 후보")
    print(f"    {'규칙':28s} {'제외':>4s} {'B제거':>5s} {'A손실':>5s} "
          f"{'>=3%손':>6s} {'>=5%손':>6s} {'>=8%손':>6s} {'정밀도':>6s} {'제외net합':>9s}")
    cands = []
    for kk in KEYS:
        vals = sorted({FE[(t["date"], t["entry_time"])][kk] for t in G30
                       if FE[(t["date"], t["entry_time"])][kk] == FE[(t["date"], t["entry_time"])][kk]})
        if len(vals) > 40:
            vals = list(np.percentile(vals, np.arange(5, 100, 5)))
        for v in vals:
            for op in ("<", ">"):
                sel = [t for t in G30
                       if (lambda x: x == x and (x < v if op == "<" else x > v))(
                           FE[(t["date"], t["entry_time"])][kk])]
                if not (4 <= len(sel) <= 30):
                    continue
                nb = sum(1 for t in sel if t["mfe"] < 1.5)
                na = len(sel) - nb
                cands.append((nb - na * 1.0, f"{kk} {op} {v:.4g}", sel, nb, na))
    seen = set()
    for _, name, sel, nb, na in sorted(cands, key=lambda z: -z[0]):
        key = name.split()[0]
        if key in seen:
            continue
        seen.add(key)
        print(f"    {name:28s} {len(sel):4d} {nb:5d} {na:5d} "
              f"{sum(1 for t in sel if t['mfe']>=3):6d} {sum(1 for t in sel if t['mfe']>=5):6d} "
              f"{sum(1 for t in sel if t['mfe']>=8):6d} {nb/len(sel)*100:5.1f}% "
              f"{sum(t['net_pct'] for t in sel):+9.2f}")
        if len(seen) >= 12:
            break
