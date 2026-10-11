"""C10 — 2단계: 상호작용 모드(M0/M1/M2) · 히스테리시스 · partial overlay.

사용: python c10_phase2.py C1b_tp10_sl08_m15
인자로 받은 CHOP 변형 하나에만 적용한다(과도한 조합 금지). 재개 가능.
"""
import pickle, sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import rlib as R
import axlib as A
import cxlib as CX
from common import summarize

HERE = Path(__file__).resolve().parent
OUT = HERE / "c8.pkl"
BASE_TAG = sys.argv[1] if len(sys.argv) > 1 else "C1b_tp10_sl08_m15"
SPEC = {
    "C1a_tp08_sl08_m15": ("C1", 0.8, 0.8, 15, False, {}),
    "C1b_tp10_sl08_m15": ("C1", 1.0, 0.8, 15, False, {}),
    "C1c_tp12_sl10_m20": ("C1", 1.2, 1.0, 20, False, {}),
    "C2a_m06": ("C2", 0.8, 0.8, 6, False, {}),
    "C2b_m09": ("C2", 0.8, 0.8, 9, False, {}),
    "C2c_m15": ("C2", 0.8, 0.8, 15, False, {}),
    "C3a_q80": ("C3", 1.2, 1.0, 15, True, {"qtl": 80}),
    "C3b_q85": ("C3", 1.2, 1.0, 15, True, {"qtl": 85}),
}
st, tp, sl, mm, vex, par = SPEC[BASE_TAG]
REG = {"K": 10, "h50": 0.40, "tp1": 0.20, "off_h50": 0.30, "off_tp1": 0.30}

ctx = R.get_ctx()
D = list(ctx.dates)
TRAIN = {d for d in D if d < "20260901"}
FEAT = CX.build(ctx.hynix_bars_3m, ctx.flags_by_idx, TRAIN)


def cx(**kw):
    c = {"strategy": st, "tp": tp, "sl": sl, "maxmin": mm, "interact": "M0",
         "hyst": False, "partial": None, "vwap_exit": vex, "params": par,
         "regime": REG, "feat": FEAT, "log": []}
    c.update(kw)
    return c


NEW = [
    (BASE_TAG + "__M1", cx(interact="M1")),
    (BASE_TAG + "__M2", cx(interact="M2")),
    (BASE_TAG + "__HYST", cx(hyst=True)),
    (BASE_TAG + "__Q1", cx(partial={"at": 1.0, "ratio": 0.5})),
    (BASE_TAG + "__Q2", cx(partial={"at": 1.2, "ratio": 0.5})),
]

state = pickle.load(open(OUT, "rb"))
t00 = time.time()
for tag, c in NEW:
    if tag in state["runs"]:
        print("%-28s (건너뜀)" % tag, flush=True)
        continue
    R.Z.set_gate(lambda f: True)
    R.Z.RELAXED_KEYS.clear()
    R.MODE["on"] = True
    t0 = time.time()
    ts = A.run("N1", ctx, D, ax=R.AX, cx=c)
    m = summarize(ts, D)
    ncx = sum(1 for t in ts if t.get("cx_on"))
    print("%-28s 거래 %3d (CHOP %2d)  복리 %9.4f  PF %.3f  MDD %7.3f  승률 %5.1f%%  (%.0fs)"
          % (tag, m["trades"], ncx, m["compound_pct"], m["pf"], m["mdd_pct"],
             m["win_rate_pct"], time.time() - t0), flush=True)
    state["runs"][tag] = ts
    state["logs"][tag] = c["log"]
    state["bat"] = list(state["bat"]) + [tag]
    pickle.dump(state, open(OUT, "wb"))
print("\n총 %.0f분" % ((time.time() - t00) / 60))
