"""R6 — 항목7 최종조합. 카테고리별 best 1개만 사용, 조합은 D 하나.
A BASE / B lock best(P2) / C size best(S1) / D = Q1 + S1.
lock·hold 는 전 구간 음수라 B 는 형식상 포함만 하고 조합에는 넣지 않는다.
"""
import pickle, sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import rlib as R
import axlib as A
from common import summarize

HERE = Path(__file__).resolve().parent
st = pickle.load(open(HERE / "r3.pkl", "rb"))
REG = st["reg"]
ctx = R.get_ctx(); D = list(ctx.dates)
NEW = [("D_Q1_S1", {"regime": REG, "partial": {"at": 1.0, "ratio": 0.5},
                    "size": 0.85, "size_mode": "noredist"})]
for tag, rx in NEW:
    if tag in st["runs"]:
        print(tag, "이미 있음"); continue
    R.Z.set_gate(lambda f: True); R.Z.RELAXED_KEYS.clear(); R.MODE["on"] = True
    t0 = time.time()
    ts = A.run("N1", ctx, D, ax=R.AX, rx=rx)
    m = summarize(ts, D)
    print("%-14s 거래 %3d  복리 %9.4f  PF %.3f  MDD %7.3f  승률 %5.1f%%  (%.0fs)"
          % (tag, m["trades"], m["compound_pct"], m["pf"], m["mdd_pct"],
             m["win_rate_pct"], time.time() - t0), flush=True)
    st["runs"][tag] = ts
    st["bat"] = list(st["bat"]) + [tag]
    pickle.dump(st, open(HERE / "r3.pkl", "wb"))
print("saved")
