# -*- coding: utf-8 -*-
"""[T] 2026-09-21 당일 N1(+C1) 리플레이. READ-ONLY.

production 무수정. data/cache 의 replay_20260921_*_1m.csv 만 읽는다.
ctx 는 0921 을 포함해 새로 빌드한다(_ctx_T.pkl).
"""
from __future__ import annotations
import sys, pickle
from pathlib import Path
sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, r"C:\Users\FURSYS\Desktop\AI-GAP 2")

HERE = Path(__file__).resolve().parent
import hengine5 as H
H._CTX_CACHE = HERE / "_ctx_T.pkl"
H._MEMO_PATH = HERE / "_memo_T.pkl"
import axlib as A          # noqa: E402  (axlib 이 위 경로를 덮어쓰므로 재설정)
H._CTX_CACHE = HERE / "_ctx_T.pkl"
H._MEMO_PATH = HERE / "_memo_T.pkl"

DAY = "20260921"
c = H.build_ctx(80)
print(f"ctx dates = {len(c.dates)}  {c.dates[0]} .. {c.dates[-1]}")
print(f"{DAY} in ctx = {DAY in c.dates}")
if DAY not in c.dates:
    raise SystemExit("0921 not in ctx")

flags = [(i, v) for i, v in c.flags_by_idx.items()
         if str(c.hynix_bars_3m['datetime'].iloc[i])[:10].replace('-', '') == DAY]
print(f"\n{DAY} 하이닉스 3분봉 플래그 {len(flags)}개")
for i, v in sorted(flags):
    print(f"  idx={i}  {c.hynix_bars_3m['datetime'].iloc[i]}  {v}")

tr = A.run("N1", c, dates=[DAY])
print(f"\nN1 거래 {len(tr)}건")
if tr:
    print("  fields:", sorted(vars(tr[0]).keys()) if hasattr(tr[0], "__dict__") else type(tr[0]))
    for t in tr:
        print(" ", t)
pickle.dump(tr, open(HERE / "_today_trades.pkl", "wb"))
