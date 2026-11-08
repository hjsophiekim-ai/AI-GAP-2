#!/usr/bin/env bash
# A(현행) / B(신안) 재생 — 날짜별 순차(-j 1). 중복 실행 금지(락).
set -u
cd "$(dirname "$0")/../.."
LOCK=/c/Users/KIMHYU~1/AppData/Local/Temp/claude/arm3_sp/out/.cg_run.lock
if ! mkdir "$LOCK" 2>/dev/null; then
  echo "!! 이미 실행 중이다 ($LOCK). 중복 실행은 pytest basetemp 가 충돌한다."
  exit 2
fi
trap 'rmdir "$LOCK" 2>/dev/null' EXIT

export WT=${WT:-/c/Users/KIMHYU~1/AppData/Local/Temp/claude/wt_chop}
R=research_20261030_tick_arm3/scripts/r1_run.py
DAYS="20261019 20261020 20261021 20261022 20261023 20261026 20261027 20261028 20261029 20261030 20261102 20261103 20261104 20261105 20261106"
BASE="--harness CG --var ARM3 --env WK_MODE=CHOP --env WK_ARM=NONE --env WK_TICKPX=0 \
      --env WK_MUA=1 --env WK_XS=1 \
      --env MACD2_T2_SHADOW_ENABLED=0 --env MACD2_P70R_SHADOW_ENABLED=0 -j 1"

echo "=== A 현행 (게이트 OFF) ==="
for d in $DAYS; do
  python $R $BASE --tag CG_A --env WK_CGATE=0 "$d" 2>&1 | grep -E "^  2026|FAIL"
done
echo "=== B 신안 (ER60<0.1223 AND gap>=0 차단) ==="
for d in $DAYS; do
  python $R $BASE --tag CG_B --env WK_CGATE=1 --env WK_CG_ER=0.1223 --env WK_CG_GAP=0.0 "$d" 2>&1 | grep -E "^  2026|FAIL"
done
echo "=== 완료 ==="
