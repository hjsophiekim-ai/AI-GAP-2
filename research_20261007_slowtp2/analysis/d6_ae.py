"""D6: '오늘 같은 날'(오전 반대 큰레그 2연속) 에서 A(N1/P3) vs E 비교. READ-ONLY."""
import glob, json, os, sys
import numpy as np
import pandas as pd

sys.stdout.reconfigure(encoding="utf-8")
REPO = r"G:/다른 컴퓨터/내 노트북 (2)/Desktop/AI-GAP 2"
ROOT = REPO + "/research_20261004_chop_staged"
O3 = REPO + "/research_20261002_sept_strategy_replay/round3_85d/replay_json"
O4 = REPO + "/research_20261002_sept_strategy_replay/round4_lockout/replay_json"
BRKD = REPO + "/research_20261003_breakout_confirm/wk/out6"
OUT = os.path.dirname(os.path.abspath(__file__))

src = open(ROOT + "/an8.py", encoding="utf-8").read().split("DAYS = sorted(")[0]
ns = {"__file__": ROOT + "/an8.py"}
exec(compile(src, "an8lib", "exec"), ns)
trades = ns["trades"]


def ap(d):
    p = f"{O3}/REAL_A_{d}.json"
    return p if os.path.exists(p) else f"{O4}/REAL_A_{d}.json"


def pb(d):
    p = f"{ROOT}/wk/out14/REAL_EARLYBRK15_{d}.json"
    return p if os.path.exists(p) else f"{BRKD}/REAL_BRK15_{d}.json"


def pc(d):
    p = f"{ROOT}/wk/out15/REAL_EARLYUPFAST_{d}.json"
    return p if os.path.exists(p) else pb(d)


def pe(d):
    p = f"{ROOT}/wk/out17/REAL_UPFASTRS_{d}.json"
    return p if os.path.exists(p) else pc(d)


DAYS = sorted({os.path.basename(p)[7:15] for p in glob.glob(O3 + "/REAL_A_*.json")})
D = pd.read_csv(OUT + "/days.csv", dtype={"day": str}).set_index("day")
rec = []
for d in DAYS:
    row = dict(day=d, pair=float(D.loc[d, "pair_min_mfe"]) if d in D.index else 0.0)
    for v, f in (("A", ap), ("E", pe)):
        tt = trades(json.load(open(f(d), encoding="utf-8")))
        row[v] = sum(t["krw"] for t in tt)
        row[v + "n"] = len(tt)
    rec.append(row)
R = pd.DataFrame(rec)
R["d"] = R.E - R.A
R.to_csv(OUT + "/ae.csv", index=False, encoding="utf-8")
print(f"# 85일 A {R.A.sum():+,.0f} / E {R.E.sum():+,.0f} / E-A {R.d.sum():+,.0f}"
      f"  (기준 A +17,227,606 · E +18,412,222)")

print("\n## '오늘 같은 날' 임계별 A vs E")
print("| 임계 | 해당일 | A 합 | E 합 | E-A | A 일평균 | E 일평균 | A 거래 | E 거래 |")
print("|---|---|---|---|---|---|---|---|---|")
for x in (0.8, 1.0, 1.2, 1.5):
    s = R[R.pair >= x]
    print(f"| >={x:.1f}% | {len(s)}일 | {s.A.sum():+,.0f} | {s.E.sum():+,.0f} | **{s.d.sum():+,.0f}** | "
          f"{s.A.mean():+,.0f} | {s.E.mean():+,.0f} | {int(s.An.sum())} | {int(s.En.sum())} |")
print("\n## 대조: 비해당일 (임계 1.0 기준)")
s = R[R.pair < 1.0]
print(f"| {len(s)}일 | A {s.A.sum():+,.0f} | E {s.E.sum():+,.0f} | **E-A {s.d.sum():+,.0f}** | "
      f"A거래 {int(s.An.sum())} | E거래 {int(s.En.sum())} |")

print("\n## 해당일 상세 (임계 1.0%)")
print("| 일자 | 쌍MFE | A | E | E-A | A건 | E건 |")
print("|---|---|---|---|---|---|---|")
for _, r in R[R.pair >= 1.0].sort_values("day").iterrows():
    print(f"| {r.day} | {r.pair:.2f}% | {r.A:+,.0f} | {r.E:+,.0f} | {r.d:+,.0f} | {int(r.An)} | {int(r.En)} |")
