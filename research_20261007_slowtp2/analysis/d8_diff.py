"""D8: 오늘형 날에서 E 가 A 에 지는 이유 분해 — 진입 누락인가 청산인가. READ-ONLY."""
import glob, json, os, sys
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
ap = lambda d: (f"{O3}/REAL_A_{d}.json" if os.path.exists(f"{O3}/REAL_A_{d}.json") else f"{O4}/REAL_A_{d}.json")
pb = lambda d: (f"{ROOT}/wk/out14/REAL_EARLYBRK15_{d}.json" if os.path.exists(f"{ROOT}/wk/out14/REAL_EARLYBRK15_{d}.json") else f"{BRKD}/REAL_BRK15_{d}.json")
pc = lambda d: (f"{ROOT}/wk/out15/REAL_EARLYUPFAST_{d}.json" if os.path.exists(f"{ROOT}/wk/out15/REAL_EARLYUPFAST_{d}.json") else pb(d))
pe = lambda d: (f"{ROOT}/wk/out17/REAL_UPFASTRS_{d}.json" if os.path.exists(f"{ROOT}/wk/out17/REAL_UPFASTRS_{d}.json") else pc(d))

D = pd.read_csv(OUT + "/days.csv", dtype={"day": str}).set_index("day")
DAYS = [d for d in sorted({os.path.basename(p)[7:15] for p in glob.glob(O3 + "/REAL_A_*.json")})
        if d in D.index and float(D.loc[d, "pair_min_mfe"]) >= 1.0]
print(f"# 오늘형 날 {len(DAYS)}일")
print("\n| 일자 | 전략 | 진입 | 방향 | 청산 | 보유분 | net% | 손익 | 사유 |")
print("|---|---|---|---|---|---|---|---|---|")
agg = {"A": [0, 0], "E": [0, 0]}
for d in DAYS:
    tr = {v: trades(json.load(open(f(d), encoding="utf-8"))) for v, f in (("A", ap), ("E", pe))}
    if sum(t["krw"] for t in tr["A"]) == sum(t["krw"] for t in tr["E"]):
        continue           # 동일일은 생략
    for v in ("A", "E"):
        for t in tr[v]:
            agg[v][0] += t["krw"]; agg[v][1] += 1
            print(f"| {d} | {v} | {t['entry'].strftime('%H:%M')} | {t['dir']} | "
                  f"{t['exit'].strftime('%H:%M')} | {t['hold_min']:.0f} | {t['net']:+.2f} | "
                  f"{t['krw']:+,.0f} | {'+'.join(dict.fromkeys(t['reasons']))} |")
    print("| | | | | | | | | |")
print(f"\n# 차이나는 날 합계: A {agg['A'][0]:+,.0f}({agg['A'][1]}건) / E {agg['E'][0]:+,.0f}({agg['E'][1]}건)")
