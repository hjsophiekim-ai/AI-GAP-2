"""C8 — CHOP MODE 배터리 (재개 가능, warm memo). 전부 실제 엔진 replay.

슬롯/일예산/거래밀림/대체진입/W1a sizing/기존 청산 우선순위/수수료·슬리피지가
모두 반영된다 — CHOP 진입은 tw3.resolve_slot 을 그대로 타고 슬롯을 소비한다.
threshold 는 train(5~8월) 분위수에서만 생성한다. READ-ONLY.
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
REG = {"K": 10, "h50": 0.40, "tp1": 0.20, "off_h50": 0.30, "off_tp1": 0.30}

ctx = R.get_ctx()
D = list(ctx.dates)
TRAIN = {d for d in D if d < "20260901"}
FEAT = CX.build(ctx.hynix_bars_3m, ctx.flags_by_idx, TRAIN)
print("ctx %d일 %s~%s · train %d일" % (len(D), D[0], D[-1], len(TRAIN)), flush=True)
print("분위수(train): VWAP|dist| q80=%.4f q85=%.4f · |gap| median=%.4f"
      % (FEAT["q"]["vwap_abs_q80"], FEAT["q"]["vwap_abs_q85"], FEAT["q"]["gap_abs_med"]),
      flush=True)


def cx(strategy, tp, sl, maxmin, *, interact="M0", hyst=False, partial=None,
       vwap_exit=False, params=None):
    return {"strategy": strategy, "tp": tp, "sl": sl, "maxmin": maxmin,
            "interact": interact, "hyst": hyst, "partial": partial,
            "vwap_exit": vwap_exit, "params": params or {},
            "regime": REG, "feat": FEAT, "log": []}


BAT = [
    ("A_BASE", None),
    # C1 SHORT CONTINUATION — 청산 3종
    ("C1a_tp08_sl08_m15", cx("C1", 0.8, 0.8, 15)),
    ("C1b_tp10_sl08_m15", cx("C1", 1.0, 0.8, 15)),
    ("C1c_tp12_sl10_m20", cx("C1", 1.2, 1.0, 20)),
    # C2 FAILED BREAKOUT REVERSAL — 보유 3종
    ("C2a_m06", cx("C2", 0.8, 0.8, 6)),
    ("C2b_m09", cx("C2", 0.8, 0.8, 9)),
    ("C2c_m15", cx("C2", 0.8, 0.8, 15)),
    # C3 VWAP MEAN REVERSION — 분위수 2종 (VWAP 접근 청산 포함)
    ("C3a_q80", cx("C3", 1.2, 1.0, 15, vwap_exit=True, params={"qtl": 80})),
    ("C3b_q85", cx("C3", 1.2, 1.0, 15, vwap_exit=True, params={"qtl": 85})),
]

state = pickle.load(open(OUT, "rb")) if OUT.exists() else {"runs": {}, "logs": {}}
state["dates"] = D
state["bat"] = [b[0] for b in BAT]


def run(tag, c):
    R.Z.set_gate(lambda f: True)
    R.Z.RELAXED_KEYS.clear()
    R.MODE["on"] = True
    t0 = time.time()
    ts = A.run("N1", ctx, D, ax=R.AX, **({"cx": c} if c else {}))
    m = summarize(ts, D)
    ncx = sum(1 for t in ts if t.get("cx_on"))
    print("%-20s 거래 %3d (CHOP %2d)  복리 %9.4f  PF %.3f  MDD %7.3f  승률 %5.1f%%  (%.0fs)"
          % (tag, m["trades"], ncx, m["compound_pct"], m["pf"], m["mdd_pct"],
             m["win_rate_pct"], time.time() - t0), flush=True)
    return ts, (c or {}).get("log", [])


t00 = time.time()
for tag, c in BAT:
    if tag in state["runs"]:
        print("%-20s (건너뜀)" % tag, flush=True)
        continue
    ts, log = run(tag, c)
    state["runs"][tag] = ts
    state["logs"][tag] = log
    pickle.dump(state, open(OUT, "wb"))
print("\n총 %.0f분 · saved c8.pkl" % ((time.time() - t00) / 60))
