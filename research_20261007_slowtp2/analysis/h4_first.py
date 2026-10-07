"""H4: '09:00 이후 첫 장중 플래그까지 걸린 시간' 이 그날의 추세성을 가르는가. READ-ONLY.
   오늘 10/07 은 57분(09:57) 이다 -- MACD 가 한 시간 동안 교차하지 않았다는 뜻.
"""
import glob, json, os, sys
import numpy as np
import pandas as pd

sys.stdout.reconfigure(encoding="utf-8")
REPO = r"G:/다른 컴퓨터/내 노트북 (2)/Desktop/AI-GAP 2"
ROOT = REPO + "/research_20261004_chop_staged"
OUT = os.path.dirname(os.path.abspath(__file__))
L = pd.read_csv(OUT + "/legs.csv", dtype={"day": str})
LB = pd.read_csv(OUT + "/label2.csv", dtype={"day": str})

first = L.sort_values("mins").groupby("day").first().reset_index()[["day", "mins", "dir", "mfe", "dur"]]
first.columns = ["day", "first_min", "first_dir", "first_mfe", "first_dur"]
first["ff"] = first.first_min - 9 * 60
print("# 85일 '첫 장중 플래그' 시각 분포")
print(first.ff.describe(percentiles=[.1, .25, .5, .75, .9, .95]).round(1).to_string())
print(f"\n오늘 10/07 = **57분** (09:57) -> 과거 분포에서 상위 {100*(first.ff<57).mean():.0f}% 지점")

print("\n## 첫 플래그가 늦을수록 그 레그가 긴가")
first["b"] = pd.cut(first.ff, [-1, 5, 15, 30, 50, 999], labels=["<=5분", "5~15분", "15~30분", "30~50분", "50분+"])
print(first.groupby("b", observed=True).agg(일수=("day", "size"), 첫레그MFE=("first_mfe", "mean"),
      중앙=("first_mfe", "median"), 지속분=("first_dur", "mean"),
      _1=("first_mfe", lambda s: f"{(s>=1.0).mean()*100:.0f}%"),
      _2=("first_mfe", lambda s: f"{(s>=2.0).mean()*100:.0f}%")).round(2).to_string())

print("\n## 그날 전체(하이닉스 기준) 과도 — 첫 플래그 지연 vs 당일 레그 수/평균진폭")
agg = L.groupby("day").agg(nleg=("mfe", "size"), legmfe=("mfe", "mean"), maxmfe=("mfe", "max")).reset_index()
M = first.merge(agg, on="day")
print(M.groupby("b", observed=True).agg(일수=("day", "size"), 당일레그수=("nleg", "mean"),
      평균진폭=("legmfe", "mean"), 최대레그=("maxmfe", "mean")).round(2).to_string())

print("\n## 거래 손익과의 관계 (A 전략)")
T = LB.merge(first[["day", "ff", "b"]], on="day")
print(T.groupby("b", observed=True).agg(거래=("krw", "size"), 손익=("krw", "sum"), 거래당=("krw", "mean"),
      레그MFE=("mfe", "mean"), _2=("mfe", lambda s: f"{(s>=2).mean()*100:.0f}%"),
      _3=("mfe", lambda s: f"{(s>=3).mean()*100:.0f}%")).round(1).to_string())

print("\n## P3 CHOP 라벨 42건만 — 첫 플래그 지연으로 가려지는가")
C = T[T.p3 == "CHOP"]
print(C.groupby("b", observed=True).agg(거래=("krw", "size"), 손익=("krw", "sum"),
      레그MFE=("mfe", "mean"), _2=("mfe", lambda s: f"{(s>=2).mean()*100:.0f}%"),
      _3=("mfe", lambda s: f"{(s>=3).mean()*100:.0f}%")).round(1).to_string())
print("\n   (오늘 10/07 은 ff=57분 -> '50분+' 구간)")
print("\n## 50분+ 인 날 목록")
for _, r in M[M.ff >= 50].sort_values("day").iterrows():
    print(f"   {r.day}  첫플래그 {int(r.first_min)//60:02d}:{int(r.first_min)%60:02d} ({int(r.ff)}분) "
          f"{r.first_dir:10s} 첫레그MFE {r.first_mfe:.2f}% 지속 {r.first_dur:.0f}분 · 당일레그 {int(r.nleg)}개")
