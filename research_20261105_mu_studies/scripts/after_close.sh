#!/bin/bash
# 장 마감 후(15:40) 실행: WAIT gap 컷 B 나머지 + 11/03 스냅샷 검증 + 11/04 A/B/C. 병렬 1.
S46="/c/Users/FURSYS/AppData/Local/Temp/claude/C--Users-FURSYS-Desktop-AI-GAP-2/46f5b540-1e0b-4dc8-b0bf-faf804cf00b0/scratchpad"
SBB="/c/Users/FURSYS/AppData/Local/Temp/claude/e15cd0182c2b0e4d/bb472455-7d2e-4dab-8f86-dec4d3d0ffa0/scratchpad"
W="/c/Users/FURSYS/AppData/Local/Temp/claude/e15cd0182c2b0e4d/abf63e6d-d10e-4d96-ad22-c76642c87888/scratchpad"
cd "$W/wg"
now=$(date +%s); target=$(date -d "15:40" +%s); [ $target -gt $now ] && sleep $((target - now))
E="--env WK_MODE=CHOP --env WK_ARM=NONE --env WK_TICKPX=0 --env WK_MUA=1 --env WK_XS=1 --env MACD2_T2_SHADOW_ENABLED=0 --env MACD2_P70R_SHADOW_ENABLED=0"
run() { WT="$SBB/wt18" REPLAY_SP="$1" ARM3_RT="$2" ARM3_OUT="$W/wg/out" python r1_run.py --harness WG --var ARM3 -j 1 "${@:3}" $E >> after.log 2>&1; }
echo "start $(date +%H:%M)" > after.log
# 1) 11/03 스냅샷 분봉 검증 (A 현행)
run "$W/d1104/sp_chk" "$SBB/mu/rt" --tag SCHK 20261103
# 2) 11/04 A / B / C (스냅샷 분봉)
run "$W/d1104/sp_1104" "$W/d1104/rt" --tag S4A 20261104
run "$W/d1104/sp_1104" "$W/d1104/rt" --tag S4B --env WK_WG_LO=400 --env WK_WG_HI=800 20261104
run "$W/d1104/sp_1104" "$W/d1104/rt" --tag S4C --env WK_WG_LO=-1 --env WK_WG_HI=1e18 20261104
# 3) B 나머지 6일 (KIS 분봉)
run "$S46/rsp" "$S46/mu_sp/rt" --tag WGB --env WK_WG_LO=400 --env WK_WG_HI=800 20261022 20261023 20261028 20261029 20261102
run "$SBB/rsp" "$SBB/mu/rt" --tag WGB --env WK_WG_LO=400 --env WK_WG_HI=800 20261103
echo "end $(date +%H:%M)" >> after.log
