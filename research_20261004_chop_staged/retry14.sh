#!/bin/bash
S="G:/다른 컴퓨터/내 노트북 (2)/Desktop/AI-GAP 2/research_20261004_chop_staged"
# 메인 큐가 끝날 때까지 대기
while ! grep -q "ALL DONE" "$S/run14b.log" 2>/dev/null; do sleep 60; done
# 실패/누락분 재시도
python - <<'PY' > "$S/jobs14r.txt"
import json,glob,os
S=os.path.abspath(".")
PREV=dict(json.load(open("rs_days.json",encoding="utf-8"))["prev"]); PREV.setdefault("20261002",("20261001","20260930"))
plan=json.load(open("ep_plan.json",encoding="utf-8"))
done={os.path.basename(p)[len("REAL_EARLYBRK15_"):-5] for p in glob.glob("wk/out14/*.json")}
miss=[d for d in plan["need"] if d not in done]
print("\n".join(f"REAL EARLYBRK15 {d} {PREV[d][0]} {PREV[d][1]}" for d in miss))
PY
cat "$S/jobs14r.txt" | xargs -P 4 -I{} bash -c 'set -- {}; "'"$S"'/job14.sh" "$1" "$2" "$3" "$4" "$5"'
echo "RETRY DONE"
