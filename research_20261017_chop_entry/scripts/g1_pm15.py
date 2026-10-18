"""±1.5% 고정청산 오버레이 (2026-10-17). READ-ONLY, 재생 아님 — 경로 기반 산술 계산.

규칙 (사전 고정, 탐색 없음):
  각 거래를 진입가 기준 **+1.5% 먼저 닿으면 익절 / −1.5% 먼저 닿으면 손절** 로 바꾼다.
  둘 다 안 닿으면 **원래 청산시각·청산가 그대로** 둔다(반대신호·장마감 청산 의미 유지).
  한 1분봉 안에서 양쪽이 다 닿으면 **불리한 쪽(손절)이 먼저** 라고 본다(보수적).
  수수료는 원래 거래의 실측 왕복비용(net - 가격수익률)을 그대로 쓴다.

한계: 청산시각이 바뀌면 자금이 풀리는 시점이 달라져 **뒤 거래의 진입 가능 여부**가 바뀔 수
있다. 이 계산은 그 2차효과를 무시한다(= 빠른 근사). 부호가 애매하면 재생으로 확인해야 한다.
"""
import glob
import json
import os
import sys

import numpy as np
import pandas as pd

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import rep2  # noqa: E402

BARS = "C:/Users/KIMHYU~1/AppData/Local/Temp/claude/G----------------2--Desktop-AI-GAP-2/4745b385-5441-4c15-97e7-446b1c980929/scratchpad/bb/sp/data"
W = rep2.LED
TP, SL = 1.5, -1.5
_B = {}


def bars(day, sym):
    k = (day, sym)
    if k not in _B:
        tag = "long" if sym == "0193T0" else "inverse"
        p = f"{BARS}/replay_{day}_{tag}_1m.csv"
        if not os.path.exists(p):
            _B[k] = None
        else:
            x = pd.read_csv(p)
            x["datetime"] = pd.to_datetime(x["datetime"].astype(str).str[:19])
            _B[k] = x.drop_duplicates("datetime", keep="last").sort_values("datetime")
    return _B[k]


def overlay(day, t):
    """원래 거래 t 를 ±1.5% 규칙으로 바꿨을 때의 손익(원)."""
    b = bars(day, t["sym"])
    px, q = t["px_in"], t["q"]
    cost = t["net"] - (t["krw"] / (q * px) * 100 - t["net"] + t["net"])      # = 0; 아래서 실측 사용
    e0 = pd.Timestamp(t["entry"]).tz_localize(None)
    e1 = pd.Timestamp(t["exit"]).tz_localize(None)
    gross_pct = t["krw"] / (q * px) * 100
    fee_pct = gross_pct - ((t["px_out"] / px - 1) * 100) if t.get("px_out") else -0.1
    if b is None:
        return t["krw"], "원본(봉없음)"
    w = b[(b.datetime > e0) & (b.datetime <= e1)]
    for r in w.itertuples():
        lo = (r.low / px - 1) * 100
        hi = (r.high / px - 1) * 100
        if lo <= SL:                                   # 보수적: 손절 먼저
            return (SL + fee_pct) / 100 * q * px, "SL1.5"
        if hi >= TP:
            return (TP + fee_pct) / 100 * q * px, "TP1.5"
    _ = cost
    return t["krw"], "원본유지"


def trades_px(o):
    """rep2.trades + 마지막 매도가(px_out) 를 붙인다 (수수료 역산용)."""
    sells = [x for x in o["orders"] if x["side"] == "SELL"]
    ts = rep2.trades(o)
    si = 0
    for t in ts:
        t["px_out"] = None
    for t in ts:
        while si < len(sells) and pd.Timestamp(sells[si]["t"]) < pd.Timestamp(t["entry"]):
            si += 1
        tail = [s for s in sells if pd.Timestamp(s["t"]) == pd.Timestamp(t["exit"])]
        if tail:
            t["px_out"] = float(tail[-1]["px"])
    return ts


def main():
    days = [x.strip() for x in open(rep2.MY + "/days.txt") if x.strip()]
    out = []
    for tag in ("BASE", "CHOP"):
        for d in days:
            p = f"{W}/{tag}_{d}.json"
            if not os.path.exists(p):
                continue
            for t in trades_px(json.load(open(p, encoding="utf-8"))):
                k, how = overlay(d, t)
                out.append(dict(strat=("E" if tag == "BASE" else "CHOP"), day=d,
                                entry=pd.Timestamp(t["entry"]).strftime("%H:%M"), sym=t["sym"],
                                orig=t["krw"], pm15=k, how=how,
                                reason="+".join(t["reasons"])))
    x = pd.DataFrame(out)
    x.to_csv(os.path.dirname(os.path.dirname(os.path.abspath(__file__))) + "/out/g1_pm15.csv",
             index=False, encoding="utf-8-sig")
    print("±1.5% 고정청산 오버레이 (경로 기반 산술, 2차효과 무시)\n")
    for strat in ("E", "CHOP"):
        s = x[x.strat == strat]
        have = sorted(s.day.unique())
        print(f"[{strat}]  원장 보유 {len(have)}일")
        print(f"  {'구간':12s} {'일수':>4s} {'거래':>4s} {'원래':>12s} {'±1.5%':>12s} {'Δ':>12s} "
              f"{'TP1.5':>5s} {'SL1.5':>5s} {'원본':>4s}")
        for lab, n in (("최근 10일", 10), ("최근 16일", 16), ("최근 20일", 20)):
            sub = [d for d in days[-n:] if d in have]
            g = s[s.day.isin(sub)]
            if not len(g):
                continue
            c = g.how.value_counts()
            print(f"  {lab:12s} {len(sub):4d} {len(g):4d} {g.orig.sum():+12,.0f} {g.pm15.sum():+12,.0f} "
                  f"{g.pm15.sum() - g.orig.sum():+12,.0f} {c.get('TP1.5', 0):5d} {c.get('SL1.5', 0):5d} "
                  f"{c.get('원본유지', 0):4d}")
        for lab, m in (("8~10월", s.day >= "20260803"), ("전체", s.day > "0")):
            g = s[m]
            c = g.how.value_counts()
            print(f"  {lab:12s} {g.day.nunique():4d} {len(g):4d} {g.orig.sum():+12,.0f} {g.pm15.sum():+12,.0f} "
                  f"{g.pm15.sum() - g.orig.sum():+12,.0f} {c.get('TP1.5', 0):5d} {c.get('SL1.5', 0):5d} "
                  f"{c.get('원본유지', 0):4d}")
        print()
    print("[최근 16일 CHOP 개별 거래]")
    g = x[(x.strat == "CHOP") & (x.day.isin(days[-16:]))].copy()
    g["Δ"] = g.pm15 - g.orig
    print(g[["day", "entry", "sym", "orig", "pm15", "Δ", "how", "reason"]].round(0).to_string(index=False))


if __name__ == "__main__":
    main()
