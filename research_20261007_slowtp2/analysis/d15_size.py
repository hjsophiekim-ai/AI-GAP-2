"""D15: 오늘형 날로 '판정된 뒤' 진입에 비중 x1.25 (DAY-RS). 1차근사. READ-ONLY.
   판정시각 = 자격을 만족시킨 2번째 플래그 시각. 그 이후 진입만 대상(미래참조 없음).
   한도/슬롯/체결가 영향은 반영 못 한다 -- 재생 전 선별용이다.
"""
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
ns = {"__file__": ROOT + "/an8.py"}; exec(compile(src, "an8lib", "exec"), ns)
trades = ns["trades"]
ap = lambda d: (f"{O3}/REAL_A_{d}.json" if os.path.exists(f"{O3}/REAL_A_{d}.json") else f"{O4}/REAL_A_{d}.json")
pb = lambda d: (f"{ROOT}/wk/out14/REAL_EARLYBRK15_{d}.json" if os.path.exists(f"{ROOT}/wk/out14/REAL_EARLYBRK15_{d}.json") else f"{BRKD}/REAL_BRK15_{d}.json")
pc = lambda d: (f"{ROOT}/wk/out15/REAL_EARLYUPFAST_{d}.json" if os.path.exists(f"{ROOT}/wk/out15/REAL_EARLYUPFAST_{d}.json") else pb(d))
pe = lambda d: (f"{ROOT}/wk/out17/REAL_UPFASTRS_{d}.json" if os.path.exists(f"{ROOT}/wk/out17/REAL_UPFASTRS_{d}.json") else pc(d))

L = pd.read_csv(OUT + "/legs.csv", dtype={"day": str})
AM_END = 11 * 60 + 30
# 판정시각: 자격(두 레그 모두 >= TH, 반대방향, 간격<=120분) 을 만족시킨 2번째 플래그
def qual_at(day, TH):
    g = L[L.day == day].sort_values("mins").reset_index(drop=True)
    am = g[g.mins < AM_END]
    for i in range(len(am) - 1):
        a, b = am.iloc[i], am.iloc[i + 1]
        if a["dir"] == b["dir"] or b["mins"] - a["mins"] > 120:
            continue
        if min(a["mfe"], b["mfe"]) >= TH:
            # 1번째 레그는 2번째 플래그 시점에 이미 끝났다 = 관측 가능
            return int(b["mins"])
    return None

DAYS = sorted({os.path.basename(p)[7:15] for p in glob.glob(O3 + "/REAL_A_*.json")})
print("| 임계 | 해당일 | 판정후 진입 | 기본 E | x1.25 추정 | 증분 | train 증분 | test 증분 |")
print("|---|---|---|---|---|---|---|---|")
for TH in (0.8, 1.0, 1.2, 1.5):
    base = bump = 0.0; n = 0; seg = {"train": 0.0, "test": 0.0}
    for d in DAYS:
        q = qual_at(d, TH)
        if q is None:
            continue
        for t in trades(json.load(open(pe(d), encoding="utf-8"))):
            m = t["entry"].hour * 60 + t["entry"].minute
            if m < q:
                continue
            n += 1; base += t["krw"]; bump += t["krw"] * 1.25
            seg["train" if d < "20260801" else "test"] += t["krw"] * 0.25
    print(f"| >={TH:.1f}% | - | {n} | {base:+,.0f} | {bump:+,.0f} | **{bump-base:+,.0f}** | "
          f"{seg['train']:+,.0f} | {seg['test']:+,.0f} |")

print("\n## 임계 1.0% — 판정후 진입 전체 (집중도 확인)")
rows = []
for d in DAYS:
    q = qual_at(d, 1.0)
    if q is None:
        continue
    for t in trades(json.load(open(pe(d), encoding="utf-8"))):
        m = t["entry"].hour * 60 + t["entry"].minute
        if m >= q:
            rows.append((d, t["entry"].strftime("%H:%M"), t["krw"] * 0.25, t["krw"]))
R = pd.DataFrame(rows, columns=["day", "at", "add", "krw"]).sort_values("add", ascending=False)

tot = R["add"].sum()
print(f"n={len(R)} 증분 {tot:+,.0f} · 양수 {int((R['add']>0).sum())} / 음수 {int((R['add']<0).sum())}")
for k in (1, 2, 3):
    print(f"  상위 {k}건 제외 → {tot - R['add'].head(k).sum():+,.0f}")
print(R.head(6).to_string(index=False))
print(R.tail(4).to_string(index=False))
