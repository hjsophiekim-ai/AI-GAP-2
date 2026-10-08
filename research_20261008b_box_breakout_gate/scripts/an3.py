"""날(day) 단위 chop/trend 판정이 신호 단위보다 나은가. READ-ONLY.

사용자 관찰: 10/07 = 전부 trend 인데 일찍 손절 / 10/08 오전 = 전부 chop 박스 왕복.
오전 09:00~11:00 하이닉스 경로로 그 날을 가를 수 있는지만 본다 (threshold 탐색 없음).
"""
import glob, json, os, sys
import numpy as np
import pandas as pd

sys.stdout.reconfigure(encoding="utf-8")
W = os.path.dirname(os.path.abspath(__file__))
ROOT = "G:/다른 컴퓨터/내 노트북 (2)/Desktop/AI-GAP 2"
sys.path.insert(0, W)
import feat as FT

TR = []
for dd in [ROOT + "/research_20261008_peaklock/out", W + "/out"]:
    for p in sorted(glob.glob(dd + "/REAL_UPFASTRS_*.json")):
        day = os.path.basename(p)[-13:-5]
        if day < "20260901":
            continue
        for t in FT.trades(json.load(open(p, encoding="utf-8"))):
            TR.append(dict(day=day, krw=t["krw"], entry=str(t["entry"])[11:16]))
TR += [dict(day="20261008", krw=-113928.0, entry="09:42"),
       dict(day="20261008", krw=-90802.0, entry="10:18")]
tr = pd.DataFrame(TR)

rows = []
for day in sorted(set(tr.day)):
    df = FT._day(day)[0]
    if df is None:
        continue
    am = df[df.datetime.dt.strftime("%H%M") <= "1100"]
    c = am.close.to_numpy()
    rng = (am.high.max() - am.low.min()) / c[0] * 100
    dd = np.abs(np.diff(c))
    pe = abs(c[-1] - c[0]) / dd.sum() if dd.sum() else 0.0
    e20 = FT.ema(c, 20)
    cross = int((np.diff(np.sign(c - e20)) != 0).sum())
    dayk = tr[tr.day == day].krw.sum()
    rows.append(dict(day=day, 오전레인지=rng, 오전PE=pe, EMA20교차=cross,
                     거래=int((tr.day == day).sum()), 손익=dayk))
o = pd.DataFrame(rows)
o["구분"] = np.where(o.오전PE >= o.오전PE.median(), "추세형(PE상위)", "왕복형(PE하위)")
pd.set_option("display.width", 200)
print(o.to_string(index=False, float_format=lambda x: "%.3f" % x))
print("\n오전 PE 중앙값 %.3f 로 23일을 반으로 가르면:" % o.오전PE.median())
g = o.groupby("구분").agg(일수=("day", "size"), 총손익=("손익", "sum"),
                          평균=("손익", "mean"), 손실일=("손익", lambda s: int((s < 0).sum())),
                          거래=("거래", "sum"))
print(g.to_string(float_format=lambda x: "{:+,.0f}".format(x)))
print("\n상관(오전PE vs 당일손익) r = %.3f / (오전레인지 vs 손익) r = %.3f / (EMA20교차 vs 손익) r = %.3f"
      % (o.오전PE.corr(o.손익), o.오전레인지.corr(o.손익), o.EMA20교차.corr(o.손익)))
print("\n9월만 r(오전PE) = %.3f   10월만 r = %.3f"
      % (o[o.day < "20261001"].오전PE.corr(o[o.day < "20261001"].손익),
         o[o.day >= "20261001"].오전PE.corr(o[o.day >= "20261001"].손익)))
