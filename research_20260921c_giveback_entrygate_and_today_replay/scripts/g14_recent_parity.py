# -*- coding: utf-8 -*-
"""[T5] 새 ctx(0921 포함) 무결성 — 최근 6영업일 거래가 출하 원장과 일치하는가."""
from __future__ import annotations
import sys
from pathlib import Path
sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, r"C:\Users\FURSYS\Desktop\AI-GAP 2")
HERE = Path(__file__).resolve().parent
import hengine5 as H
H._CTX_CACHE = HERE / "_ctx_T.pkl"; H._MEMO_PATH = HERE / "_memo_T.pkl"
import axlib as A                                  # noqa: E402
H._CTX_CACHE = HERE / "_ctx_T.pkl"; H._MEMO_PATH = HERE / "_memo_T.pkl"
import k1_core as K                                # noqa: E402

c = H.build_ctx(80)
DS = [d for d in c.dates if d >= "20260911" and d != "20260921"]
tr = A.run("N1", c, dates=DS)
led = [t for t in K.load() if t["date"] in set(DS)]
print(f"검증창 {DS}")
print(f"  새 ctx 리플레이 {len(tr)}건 / 출하 원장(N1+C1) {len(led)}건")
g = {(t["date"], t["entry_time"][11:16]): t for t in tr}
l = {(t["date"], t["entry_time"][11:16]): t for t in led}
print(f"  진입키 일치 {len(set(g) & set(l))} / 리플레이 전용 {sorted(set(g)-set(l))} / 원장 전용 {sorted(set(l)-set(g))}")
bad = 0
for k in sorted(set(g) & set(l)):
    a, b = g[k], l[k]
    de = abs(a["entry_price"] - b["entry_price"])
    dn = abs(a["net_pct"] - b["net_pct"])
    same_exit = a["exit_reason"] == b["exit_reason"]
    flag = "" if (de < 0.51 and dn < 0.005 and same_exit) else "  <== 불일치"
    if flag:
        bad += 1
    print(f"    {k[0]} {k[1]}  진입가 {a['entry_price']:,.0f}/{b['entry_price']:,.0f}  "
          f"net {a['net_pct']:+.3f}/{b['net_pct']:+.3f}  {a['exit_reason']}/{b['exit_reason']}{flag}")
print(f"  불일치 {bad}건")
