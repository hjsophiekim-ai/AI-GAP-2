"""b3run — 변형 실행기.  python b3run.py BASE | <variant ...>

변형 정의 (공통: SL 1.0 / max-hold 20분 / SHADOW-BASE SLOW detector):
  T10/T11/T12       : B3 단독 (P3/Y3 OFF), TP 1.0/1.1/1.2
  P3_T10            : 현재 P3 stack (B3 TP1.0 + P3 trig1.0/6분 + Y3)
  P3A_T11/P3A_T12   : B3 TP만 변경, P3 trig 1.0 유지  (판정용)
  P3B_T11/P3B_T12   : P3 trig 도 TP 와 같이 이동      (참고용)
"""
import pickle
import sys
import time

import b3lib as L
from common import summarize

VAR = {
    "T10": dict(tp=1.0, p3=False, y3=False),
    "T11": dict(tp=1.1, p3=False, y3=False),
    "T12": dict(tp=1.2, p3=False, y3=False),
    "P3_T10": dict(tp=1.0, p3=True, p3_trig=1.0, y3=True),
    "P3A_T11": dict(tp=1.1, p3=True, p3_trig=1.0, y3=True),
    "P3A_T12": dict(tp=1.2, p3=True, p3_trig=1.0, y3=True),
    "P3B_T11": dict(tp=1.1, p3=True, p3_trig=1.1, y3=True),
    "P3B_T12": dict(tp=1.2, p3=True, p3_trig=1.2, y3=True),
    "R0": dict(tp=1.0, p3=True, p3_trig=1.0, y3=True),
    "R1": dict(tp=1.0, p3=True, p3_trig=1.0, y3=True, late="R1"),
    "R2": dict(tp=1.0, p3=True, p3_trig=1.0, y3=True, late="R2"),
    "R3": dict(tp=1.0, p3=True, p3_trig=1.0, y3=True, late="R3"),
    "M1": dict(tp=1.0, p3=True, p3_trig=1.0, y3=True, y3mode="PARTIAL"),
    "M2": dict(tp=1.0, p3=True, p3_trig=1.0, y3=True, y3mode="OR"),
    "ALL_P3": dict(tp=1.0, p3=True, p3_trig=1.0, y3=True, all=True),
    "ALL_B3": dict(tp=1.0, p3=False, y3=False, all=True),
    "S0": dict(tp=1.0, p3=True, p3_trig=1.0, y3=True),
    "S1": dict(tp=1.0, p3=True, p3_trig=1.0, y3=True, fs_stop=1.0),
    "S2": dict(tp=1.0, p3=True, p3_trig=1.0, y3=True, fs_stop=1.1),
}
strict = "--le" not in sys.argv
SFX = "" if L.NDAYS == 80 else f"_d{L.NDAYS}"
names = [a for a in sys.argv[1:] if not a.startswith("--")]

for name in names:
    t0 = time.time()
    if name in ("BASE", "BASE2"):
        ts = L.run(None)
    else:
        base = pickle.load(open(L.HERE / f"out_BASE{SFX}.pkl", "rb"))["trades"]
        cfg = dict(sl=1.0, hold=20.0, p3_min=6.0, p3_trig=1.0)
        cfg.update(VAR[name])
        cfg["regime"] = L.make_regime(base, strict=strict)
        if cfg.pop("all", False):
            cfg["regime"] = lambda _ts: "CHOP"
        ts = L.run(cfg)
    m = summarize(ts, L.DATES)
    tag = name + ("" if strict else "_le") + SFX
    print("%-8s 거래 %3d  복리 %9.4f  PF %.4f  MDD %7.3f  승률 %.2f%%  (%.0fs)"
          % (tag, m["trades"], m["compound_pct"], m["pf"], m["mdd_pct"], m["win_rate_pct"],
             time.time() - t0), flush=True)
    pickle.dump({"trades": ts, "m": m}, open(L.HERE / f"out_{tag}.pkl", "wb"))
