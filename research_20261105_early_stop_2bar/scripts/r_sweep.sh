#!/usr/bin/env bash
# 30분 경계 민감도 — 판정이 바뀌는 날만 재생한다.
set -u
R=research_20261030_tick_arm3/scripts/r1_run.py
B="--harness SLH --var ARM3 --env WK_MODE=CHOP --env WK_ARM=NONE --env WK_TICKPX=0 \
   --env MACD2_T2_SHADOW_ENABLED=0 --env MACD2_P70R_SHADOW_ENABLED=0 --env WK_SLHOLD=1 -j 1"
run() { tag=$1; min=$2; mua=$3; shift 3; \
  python $R $B --tag "$tag" --env WK_SLHOLD_MIN="$min" --env WK_MUA="$mua" --env WK_XS="$mua" "$@" 2>&1 | tail -3; }

echo "=== MIN=10 (7일) ==="
run SLH_M10 10 0 20260803 20260805 20260821 20260827 20260831 20261012 20261014
echo "=== MIN=15 (2일) ==="
run SLH_M15 15 0 20260803 20260831
echo "=== MIN=25 (1일) ==="
run SLH_M25 25 0 20260831
echo "=== MIN=35 (3일) ==="
run SLH_M35 35 0 20260810 20260812
run SLH_M35 35 1 20261021
echo "=== MIN=45 (1일) ==="
run SLH_M45 45 0 20260824
echo "=== MIN=120 (2일) ==="
run SLH_M120 120 0 20260903 20260915
echo "=== SWEEP DONE ==="
