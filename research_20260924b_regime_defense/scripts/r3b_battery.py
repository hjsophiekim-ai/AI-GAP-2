"""R3b — 배터리 (재개 가능 · memo warm). 변형마다 즉시 저장한다.

warm memo 사용 근거: memo 는 봉 인덱스 기준의 **진입측 판정**(TEG/quality)만
캐시하고 rx 는 사이징/청산만 바꾸므로 캐시 내용이 변형과 무관하다. 그래도
가정에 기대지 않기 위해 **BASE 를 맨 앞(cold)과 맨 뒤(warm)에 각각 돌려
거래집합이 완전히 같은지** 확인한다. 어긋나면 그 자리에서 중단한다.
"""
import pickle, sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import rlib as R
import axlib as A, hengine5 as H

HERE = Path(__file__).resolve().parent
OUT = HERE / "r3.pkl"
REG = {"K": 10, "h50": 0.40, "tp1": 0.20}
BAT = [
    ("P0_BASE",        None),
    ("P1_lock10_05",   {"regime": REG, "lock": {"arm": 1.0, "keep": 0.5}}),
    ("P2_lock12_06",   {"regime": REG, "lock": {"arm": 1.2, "keep": 0.6}}),
    ("P3_lock15_075",  {"regime": REG, "lock": {"arm": 1.5, "keep": 0.75}}),
    ("H1_hold45",      {"regime": REG, "hold_min": 45}),
    ("H2_hold60",      {"regime": REG, "hold_min": 60}),
    ("H3_hold75",      {"regime": REG, "hold_min": 75}),
    ("S1_size085",     {"regime": REG, "size": 0.85, "size_mode": "noredist"}),
    ("S2_size075",     {"regime": REG, "size": 0.75, "size_mode": "noredist"}),
    ("S1r_size085_rd", {"regime": REG, "size": 0.85, "size_mode": "redist"}),
    ("S2r_size075_rd", {"regime": REG, "size": 0.75, "size_mode": "redist"}),
    ("Q1_part10",      {"regime": REG, "partial": {"at": 1.0, "ratio": 0.5}}),
    ("Q2_part12",      {"regime": REG, "partial": {"at": 1.2, "ratio": 0.5}}),
    ("Q3_part15",      {"regime": REG, "partial": {"at": 1.5, "ratio": 0.5}}),
    ("P0_BASE_END",    None),
]

state = pickle.load(open(OUT, "rb")) if OUT.exists() else {"runs": {}}
ctx = R.get_ctx(); D = list(ctx.dates)
state["dates"] = D; state["reg"] = REG; state["bat"] = [b[0] for b in BAT if b[0] != "P0_BASE_END"]
print("ctx %d일 %s~%s · 이미 완료 %d개\n" % (len(D), D[0], D[-1], len(state["runs"])), flush=True)


def run_warm(rx, tag):
    """memo 는 유지하고 Q3_BASE 만 공유 — 첫 런에서 채워지고 이후 재사용."""
    R.Z.set_gate(lambda f: True); R.Z.RELAXED_KEYS.clear(); R.MODE["on"] = True
    t0 = time.time()
    ts = A.run("N1", ctx, D, ax=R.AX, **({"rx": rx} if rx else {}))
    from common import summarize
    m = summarize(ts, D)
    print("%-18s 거래 %3d  복리 %9.4f  PF %.3f  MDD %7.3f  승률 %5.1f%%  (%.0fs)"
          % (tag, m["trades"], m["compound_pct"], m["pf"], m["mdd_pct"],
             m["win_rate_pct"], time.time() - t0), flush=True)
    return ts


t00 = time.time()
for tag, rx in BAT:
    if tag in state["runs"]:
        print("%-18s (건너뜀 — 이미 있음)" % tag, flush=True)
        continue
    ts = run_warm(rx, tag)
    state["runs"][tag] = ts
    pickle.dump(state, open(OUT, "wb"))

if "P0_BASE" in state["runs"] and "P0_BASE_END" in state["runs"]:
    same = R.sig(state["runs"]["P0_BASE"]) == R.sig(state["runs"]["P0_BASE_END"])
    print("\nBASE 앞/뒤 동일성(warm memo 안전성):", "완전 일치" if same else "불일치!! 신뢰 불가")
    state["warm_safe"] = same
    pickle.dump(state, open(OUT, "wb"))
print("총 %.0f분 · saved r3.pkl" % ((time.time() - t00) / 60))
