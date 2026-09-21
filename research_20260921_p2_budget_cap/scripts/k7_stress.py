# -*- coding: utf-8 -*-
"""§14 실거래 스트레스 — 슬리피지 / 체결지연. READ-ONLY.

슬리피지: 왕복 추가비용을 net_pct 에서 직접 차감 (기존 C1 연구와 같은 방식).
체결지연: ctx 의 실제 1분봉으로 진입·청산 체결가를 N분 뒤로 옮겨 net 을 재계산.
  주의 — 원장 net_pct 는 TP1 부분익절이 섞인 합성값이다(158건 중 21건).
  그 21건은 청산레그 지연이 과대반영되는 근사다. 아래에 건수를 표시한다.
"""
import sys, pickle
import numpy as np, pandas as pd
sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, r"C:\Users\FURSYS\Desktop\AI-GAP 2")
import k1_core as K
from app.trading.macd2.worker import _net_return_pct

raw = pickle.load(open("_ctx_B.pkl", "rb"))
D = raw["dates"]
Q = {}
for sym, df in raw["quotes_1m"].items():
    w = df.sort_values("datetime").reset_index(drop=True)
    Q[sym] = (pd.DatetimeIndex(w["datetime"]).asi8, w["close"].astype(float).to_numpy())

TS = K.load()
P2F = K.make_extra(1.05, 0.25, 1.00)


def px_at(sym, when):
    ts, px = Q[sym]
    i = int(np.searchsorted(ts, pd.Timestamp(when).value, side="right"))
    return float(px[i - 1]) if i else None


def shifted(trades, delay_min=0, slip_pct=0.0):
    out = []
    approx = 0
    for t in trades:
        net = t["net_pct"]
        if delay_min:
            e2 = px_at(t["symbol"], pd.Timestamp(t["entry_time"]) + pd.Timedelta(minutes=delay_min))
            x2 = px_at(t["symbol"], pd.Timestamp(t["exit_time"]) + pd.Timedelta(minutes=delay_min))
            if e2 and x2:
                d = (_net_return_pct(t["symbol"], e2, x2, 1)
                     - _net_return_pct(t["symbol"], t["entry_price"], t["exit_price"], 1))
                net = net + d
                if t["tp1_hit"]:
                    approx += 1
        s = dict(t); s["net_str"] = net - slip_pct
        out.append(s)
    return out, approx


A0 = K.size_chain(TS, None)
B0 = K.size_chain(TS, P2F)
base_up = K.krw_pnl(B0, D) - K.krw_pnl(A0, D)
print(f"무스트레스 기준: A={K.krw_pnl(A0, D):,.0f}  P2={K.krw_pnl(B0, D):,.0f}  "
      f"uplift={base_up:+,.0f} KRW\n")

print("=" * 100)
print("§14  실거래 스트레스 (DAILY_CAPITAL 3,000만원 cap 항상 적용)")
print("=" * 100)
print(f"{'시나리오':22s} {'A 78d':>13s} {'P2 78d':>13s} {'uplift':>11s} "
      f"{'30d uplift':>11s} {'P2 PF':>6s} {'P2 MDD%':>8s} {'근사건수':>7s}")
print("-" * 100)
rows = []
for tag, dly, slp in (("기준 (무스트레스)", 0, 0.0),
                      ("슬리피지 +0.10%", 0, 0.10),
                      ("슬리피지 +0.20%", 0, 0.20),
                      ("슬리피지 +0.30%", 0, 0.30),
                      ("체결지연 +1분", 1, 0.0),
                      ("체결지연 +3분", 3, 0.0),
                      ("+3분 & 슬립 0.20%", 3, 0.20),
                      ("+3분 & 슬립 0.30%", 3, 0.30)):
    st, ap = shifted(TS, dly, slp)
    a = K.size_chain(st, None, net_key="net_str")
    b = K.size_chain(st, P2F, net_key="net_str")
    u78 = K.krw_pnl(b, D) - K.krw_pnl(a, D)
    u30 = K.krw_pnl(b, D[-30:]) - K.krw_pnl(a, D[-30:])
    rows.append((tag, u78, u30))
    print(f"{tag:22s} {K.krw_pnl(a, D):13,.0f} {K.krw_pnl(b, D):13,.0f} {u78:+11,.0f} "
          f"{u30:+11,.0f} {K.pf(b, D):6.3f} {K.mdd_krw(b, D):8.2f} {ap:7d}")

print("-" * 100)
ok02 = [u for t, u, _ in rows if "0.20" in t and "3분" not in t]
ok3 = [u for t, u, _ in rows if t == "체결지연 +3분"]
print(f"  채택조건 19 (슬리피지 +0.2% 후 uplift >= 0): "
      f"{'통과' if ok02 and ok02[0] >= 0 else '실패'}  ({ok02[0]:+,.0f} KRW)")
print(f"  채택조건 20 (체결지연 +3분 후 uplift >= 0): "
      f"{'통과' if ok3 and ok3[0] >= 0 else '실패'}  ({ok3[0]:+,.0f} KRW)")
print(f"  30일 창에서도 전부 비음수: "
      f"{'예' if all(u30 >= 0 for _, _, u30 in rows) else '아니오'}")
