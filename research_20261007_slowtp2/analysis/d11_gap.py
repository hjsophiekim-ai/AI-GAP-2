"""D11: 오늘형 날 E-A 격차 분해 = ① E가 안 들어간 거래 ② 같은 신호를 늦게 산 거래. READ-ONLY."""
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
ap = lambda d: (f"{O3}/REAL_A_{d}.json" if os.path.exists(f"{O3}/REAL_A_{d}.json") else f"{O4}/REAL_A_{d}.json")
pb = lambda d: (f"{ROOT}/wk/out14/REAL_EARLYBRK15_{d}.json" if os.path.exists(f"{ROOT}/wk/out14/REAL_EARLYBRK15_{d}.json") else f"{BRKD}/REAL_BRK15_{d}.json")
pc = lambda d: (f"{ROOT}/wk/out15/REAL_EARLYUPFAST_{d}.json" if os.path.exists(f"{ROOT}/wk/out15/REAL_EARLYUPFAST_{d}.json") else pb(d))
pe = lambda d: (f"{ROOT}/wk/out17/REAL_UPFASTRS_{d}.json" if os.path.exists(f"{ROOT}/wk/out17/REAL_UPFASTRS_{d}.json") else pc(d))

D = pd.read_csv(OUT + "/days.csv", dtype={"day": str}).set_index("day")
L = pd.read_csv(OUT + "/legs.csv", dtype={"day": str})
ALL = sorted({os.path.basename(p)[7:15] for p in glob.glob(O3 + "/REAL_A_*.json")})

for TH in (1.0, 1.2, 1.5):
    DAYS = [d for d in ALL if d in D.index and float(D.loc[d, "pair_min_mfe"]) >= TH]
    skip_n = skip_krw = late_n = late_krw = 0
    extra_n = extra_krw = 0
    rows = []
    for d in DAYS:
        A = trades(json.load(open(ap(d), encoding="utf-8")))
        E = trades(json.load(open(pe(d), encoding="utf-8")))
        # 같은 신호 매칭: 진입시각 차이 <=20분 & 같은 방향
        usedE = set()
        for a in A:
            m = None
            for j, b in enumerate(E):
                if j in usedE or b["dir"] != a["dir"]:
                    continue
                if abs((b["entry"] - a["entry"]).total_seconds()) <= 20 * 60:
                    m = j
                    break
            if m is None:
                skip_n += 1; skip_krw += a["krw"]
                rows.append((d, "E 미진입", a["entry"].strftime("%H:%M"), a["dir"], a["krw"], 0))
            else:
                usedE.add(m)
                b = E[m]
                late_n += 1; late_krw += b["krw"] - a["krw"]
                if abs(b["krw"] - a["krw"]) > 1000:
                    rows.append((d, "같은신호", a["entry"].strftime("%H:%M") + "->" + b["entry"].strftime("%H:%M"),
                                 a["dir"], a["krw"], b["krw"]))
        for j, b in enumerate(E):
            if j not in usedE:
                extra_n += 1; extra_krw += b["krw"]
                rows.append((d, "E 단독진입", b["entry"].strftime("%H:%M"), b["dir"], 0, b["krw"]))
    print(f"\n## 임계 {TH}% — 오늘형 {len(DAYS)}일")
    print(f"| 구분 | 건수 | A 손익 | E 손익 | E-A |")
    print(f"|---|---|---|---|---|")
    print(f"| E 가 안 들어간 A 거래 | {skip_n} | {skip_krw:+,.0f} | 0 | **{-skip_krw:+,.0f}** |")
    print(f"| 같은 신호(진입가/수량 차이만) | {late_n} | - | - | **{late_krw:+,.0f}** |")
    print(f"| E 단독 진입 | {extra_n} | 0 | {extra_krw:+,.0f} | **{extra_krw:+,.0f}** |")
    print(f"| 합계 | | | | **{-skip_krw + late_krw + extra_krw:+,.0f}** |")
    if TH == 1.5:
        print("\n### 임계 1.5% 상세")
        print("| 일자 | 구분 | 진입 | 방향 | A | E |")
        print("|---|---|---|---|---|---|")
        for r in rows:
            print(f"| {r[0]} | {r[1]} | {r[2]} | {r[3]} | {r[4]:+,.0f} | {r[5]:+,.0f} |")
