"""재생 하네스에 먹일 'MU 진입 결정시각' 목록을 만든다 (research only).

    python m4_plan.py S2 > ...    # 기본 S2 = production XS 의 STRONG 정의 그대로

출력: {ARM3_OUT}/mupri_plan_<SET>.json  =  {"20261019": [{"decide":"10:06","dir":"UP_RED",...}], ...}
결정시각은 MU 플래그봉 종료 + 2분 (MU Assist T+2 와 같은 자리).
"""
import json
import os
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.stdout.reconfigure(encoding="utf-8")
OUT = ROOT / "research_20261104_mu_primary" / "out"
PLAN_DIR = Path(os.environ.get("ARM3_OUT", r"C:/Users/KIMHYU~1/AppData/Local/Temp/claude/arm3_sp/out"))

SET = (sys.argv[1] if len(sys.argv) > 1 else "S2").upper()

d = pd.read_csv(OUT / "mu_flags.csv")
t = d[d["tradable"] & d["entry_px"].notna()].copy()
C = t["mu_state"] == "CONFIRM"
xs = t["xs"] == "MATCH"
ex = t["hy_gap_expand"] == True        # noqa: E712
opp = t["hy_gap_opp"] <= 400
nb = t["hy_breakout"] == False         # noqa: E712
sc = t["hy_score"] < 45

MASKS = {
    "B_USER": C | ((t["mu_state"] == "STALL") & xs & opp),
    "S0": C,
    "S1": C & xs,
    "S2": C & xs & ex,
    "S3": C & xs & ex & opp,
    "S4": C & xs & ex & nb & sc,
}
sel = t[MASKS[SET]].sort_values(["day", "decide"])

plan: dict[str, list] = {}
for _, r in sel.iterrows():
    day = str(r["day"])
    plan.setdefault(day, []).append({
        "decide": r["decide"], "dir": "UP_RED" if r["dir"] == "RED" else "DOWN_BLUE",
        "key": f"{day}_{r['flag_end'].replace(':', '')}_{r['dir']}",
        "mu_state": r["mu_state"], "cttr": None if pd.isna(r["cttr"]) else float(r["cttr"]),
    })

PLAN_DIR.mkdir(parents=True, exist_ok=True)
p = PLAN_DIR / f"mupri_plan_{SET}.json"
p.write_text(json.dumps(plan, ensure_ascii=False, indent=1), encoding="utf-8")
print(f"[{SET}] {len(sel)}건 / {len(plan)}일 -> {p}")
for day in sorted(plan):
    print(f"  {day}  " + "  ".join(f"{x['decide']}{x['dir'][:2]}" for x in plan[day]))
