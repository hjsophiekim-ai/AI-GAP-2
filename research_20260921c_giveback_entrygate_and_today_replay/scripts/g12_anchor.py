# -*- coding: utf-8 -*-
"""[T3] 셋업 검증 — 새 ctx(0921 포함)에서 N1 78일 앵커가 재현되는가."""
from __future__ import annotations
import sys
from pathlib import Path
sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, r"C:\Users\FURSYS\Desktop\AI-GAP 2")
HERE = Path(__file__).resolve().parent
import hengine5 as H
H._CTX_CACHE = HERE / "_ctx_T.pkl"
H._MEMO_PATH = HERE / "_memo_T.pkl"
import axlib as A                                  # noqa: E402
H._CTX_CACHE = HERE / "_ctx_T.pkl"
H._MEMO_PATH = HERE / "_memo_T.pkl"

c = H.build_ctx(80)
D78 = c.dates[:78]
print(f"ctx {len(c.dates)}일  전체 {c.dates[0]}..{c.dates[-1]}")
print(f"앵커 창 78일 {D78[0]}..{D78[-1]}")
tr = A.run("N1", c, dates=D78)
m = H.metrics(tr, D78)
print(f"N1 78일 거래수 = {len(tr)}  (기대 158)")
print("metrics:", {k: (round(v, 4) if isinstance(v, float) else v) for k, v in m.items()})
