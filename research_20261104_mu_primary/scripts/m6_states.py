"""MU 원신호 변형의 '계획 -> 주입 -> 체결 / 차단사유' 분해 (research only).

    python m6_states.py MUPRI_M1_S2 S2 [MUPRI_M2_S2 S2 ...]

주의: 재생 산출의 gates/sig 로그에는 전일(P) 선재생분이 섞인다 — 반드시 그 날로 거른다.
"""
import json
import os
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.stdout.reconfigure(encoding="utf-8")
OUT = Path(os.environ.get("ARM3_OUT", r"C:/Users/KIMHYU~1/AppData/Local/Temp/claude/arm3_sp/out"))
DAYS = ["20261019", "20261020", "20261021", "20261022", "20261023",
        "20261026", "20261027", "20261028", "20261029", "20261030"]


def report(tag, plan_set):
    plan = json.loads((OUT / f"mupri_plan_{plan_set}.json").read_text(encoding="utf-8"))
    n_plan = sum(len(v) for v in plan.values())
    fired, blocked, filled = 0, Counter(), 0
    by_state = Counter()
    fill_state = Counter()
    for day in DAYS:
        f = OUT / f"{tag}_{day}.json"
        if not f.exists():
            continue
        js = json.loads(f.read_text(encoding="utf-8"))
        iso = f"{day[:4]}-{day[4:6]}-{day[6:]}"
        log = json.loads((OUT / f"{tag}_{day}.json.tmp.mupri.json").read_text(encoding="utf-8"))["log"]
        sid_state = {}
        for e in log:
            if not e.get("fired"):
                continue
            fired += 1
            by_state[e.get("mu_state", "?")] += 1
            fb = e["flag_bar"]
            sid_state[f"{fb[:10].replace('-', '')}_{fb[11:19].replace(':', '')}_{e['dir']}"
                      ":TW2_3SLOT_CONFIRM"] = e.get("mu_state", "?")
        # 한 MU 후보가 ledger 에 여러 행으로 남는다(E 돌파대기 WAITING + ':E_BRK' 체결).
        # sid 접두사로 묶어 **최종 상태 하나**만 센다.
        final: dict[str, str] = {}
        for row in js["sig"]:
            sid = row.get("signal_id", "")
            base = next((k for k in sid_state if sid.startswith(k)), None)
            if base is None or not str(row.get("detected_at", "")).startswith(iso):
                continue
            res = row.get("order_result")
            if res == "EXECUTED":
                final[base] = "EXECUTED"
            elif final.get(base) != "EXECUTED" and res != "WAITING":
                final[base] = row.get("block_reason") or res or "?"
        for base, st in final.items():
            if st == "EXECUTED":
                filled += 1
                fill_state[sid_state[base]] += 1
            else:
                blocked[st] += 1
    print(f"\n[{tag}]  계획 {n_plan}건 -> 주입 {fired}건 -> 체결 {filled}건")
    print(f"   주입 MU상태: {dict(by_state)}   체결 MU상태: {dict(fill_state)}")
    print("   차단 사유:")
    for k, v in blocked.most_common():
        print(f"     {k:46s} {v:4d}")


args = sys.argv[1:]
for i in range(0, len(args), 2):
    report(args[i], args[i + 1])
