# -*- coding: utf-8 -*-
"""[P5] 결정적 검증 — 게이트를 **엔진에 직접 물려** 대체 진입까지 재현.

removal-only 는 슬롯이 비어 생겼을 대체 진입을 재현하지 못한다.
hengine5.run_chain(gate=...) 훅은 승인 직전에 호출되므로, 여기서 거절하면
그 슬롯이 그대로 남아 뒤의 플래그가 진입할 수 있다 = production 과 같은 의미.

게이트 판정은 `decision_at`(=진입시각) **이전** 1분봉만 쓴다.
"""
from __future__ import annotations
import sys, pickle
from pathlib import Path
import numpy as np, pandas as pd
sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, r"C:\Users\FURSYS\Desktop\AI-GAP 2")
HERE = Path(__file__).resolve().parent
import hengine5 as H
import axlib as A
import k1_core as K
import t1_path as P
from app.trading.macd2.models import Direction

c = A.ctx(78)
D = c.dates
W30, OOS = D[-30:], D[:48]
B3T = pd.DatetimeIndex(c.hynix_bars_3m["datetime"])
ETF = {"0193T0": None, "0197X0": None}
for s in ETF:
    b = P.bars(s)
    ETF[s] = (pd.to_numeric(b["close"], errors="coerce").to_numpy(float),
              pd.DatetimeIndex(b["datetime"]))
SYM = {Direction.UP_RED: "0193T0", Direction.DOWN_BLUE: "0197X0"}


def etf_follow(info) -> float:
    """플래그봉 시작 ~ 진입 직전까지 보유ETF 수익률 %. 구할 수 없으면 nan."""
    t0 = B3T[info["flag_idx"]]
    ent = pd.Timestamp(info["decision_at"])
    cl, ts = ETF[SYM[info["direction"]]]
    i0 = ts.searchsorted(t0, side="left")
    i1 = ts.searchsorted(ent, side="left")          # ent 봉 제외 = 선취 차단
    if i1 - i0 < 3:
        return float("nan")
    o, cc = cl[i0], cl[i1 - 1]
    return float((cc - o) / o * 100.0) if o > 0 else float("nan")


REJ = []


def make_gate(thr):
    def gate(info):
        v = etf_follow(info)
        REJ.append((info["date"], str(info["decision_at"]), v))
        if v == v and v <= thr:
            return "CONFIRM_ETF_NO_FOLLOW"
        return None
    return gate


def metrics(tr, W):
    rows = []
    for t in tr:
        rows.append({"date": t["date"], "session": t["session"], "slot": t["slot_number"],
                     "direction": t["direction"], "entry_time": t["entry_time"],
                     "exit_time": t["exit_time"], "entry_price": t["entry_price"],
                     "exit_price": t["exit_price"], "exit_reason": t["exit_reason"],
                     "net_pct": t["net_pct"], "peak_net_pct": t["peak_net_pct"],
                     "mae_net_pct": t["mae_net_pct"], "entry_chop": t["entry_chop"],
                     "tp1_hit": t["tp1_hit"], "hold_minutes": t["hold_minutes"],
                     "w1a_ref": t["w1a"],
                     "symbol": "0193T0" if t["direction"] == "UP_RED" else "0197X0"})
    rows.sort(key=lambda x: (x["date"], x["entry_time"]))
    sz = K.size_chain(rows, None)
    return sz


print("=" * 118)
print("[P5] 엔진 게이트 — 대체 진입 재현 포함")
print("=" * 118)
base_tr = A.run("N1", c, dates=D)
BASE = metrics(base_tr, D)
print(f"  BASE(게이트 없음) 거래 {len(BASE)}건  "
      f"30일 {K.krw_pnl(BASE,W30):,.0f} / OOS48 {K.krw_pnl(BASE,OOS):,.0f} / "
      f"78일 {K.krw_pnl(BASE,D):,.0f}  PF78 {K.pf(BASE,D):.3f}")
b30, bo, b78 = K.krw_pnl(BASE, W30), K.krw_pnl(BASE, OOS), K.krw_pnl(BASE, D)
RUN8 = sum(1 for t in BASE if t["peak_net_pct"] >= 8)
print(f"  BASE runner(peak>=8%) {RUN8}건")
print()
print(f"  {'임계 ETF추종<=X%':>16s} {'거래':>4s} {'30d uplift':>12s} {'OOS48':>12s} {'78d':>12s} "
      f"{'PF78':>6s} {'MDD78':>7s} {'runner':>6s} {'동시양수':>6s}")
print("  " + "-" * 110)
for thr in (-0.10, 0.0, 0.10):
    REJ.clear()
    tr = A.run("N1", c, dates=D, gate=make_gate(thr))
    sz = metrics(tr, D)
    r8 = sum(1 for t in sz if t["peak_net_pct"] >= 8)
    a, b, cc = K.krw_pnl(sz, W30) - b30, K.krw_pnl(sz, OOS) - bo, K.krw_pnl(sz, D) - b78
    print(f"  {thr:16.2f} {len(sz):4d} {a:+12,.0f} {b:+12,.0f} {cc:+12,.0f} "
          f"{K.pf(sz,D):6.3f} {K.mdd_krw(sz,D):7.2f} {r8:6d} "
          f"{'OK' if a>0 and b>0 else '':>6s}")
    pickle.dump(sz, open(HERE / f"_p5_{thr}.pkl", "wb"))
