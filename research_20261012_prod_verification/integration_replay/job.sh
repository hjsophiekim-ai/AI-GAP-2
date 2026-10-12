#!/bin/bash
tag=$1; D=$2; P=$3; P2=$4
out=C:/Users/FURSYS/AppData/Local/Temp/claude/C--Users-FURSYS-Desktop-AI-GAP-2/4c76d5c5-d1f9-46cd-b140-2acac83bada0/scratchpad/rp/out/REAL_${tag}_${D}.json
[ -s "$out" ] && exit 0
case $tag in P3) M=P3; SL=0;; SLOW) M=P3; SL=1;; CHOP) M=CHOP; SL=1;; CHOP0) M=CHOP; SL=0;; esac
cd C:/Users/FURSYS/AppData/Local/Temp/claude/C--Users-FURSYS-Desktop-AI-GAP-2/4c76d5c5-d1f9-46cd-b140-2acac83bada0/scratchpad/wt_int
WK_D=$D WK_P=$P WK_P2=$P2 WK_FILL=REAL WK_VAR=A WK_PRODMODE=$M MACD2_SLOW_TP2_ENABLED=$SL REPLAY_SP=C:/Users/FURSYS/AppData/Local/Temp/claude/C--Users-FURSYS-Desktop-AI-GAP-2/4c76d5c5-d1f9-46cd-b140-2acac83bada0/scratchpad/rp WK_OUT=$out.tmp PYTHONIOENCODING=utf-8 python -m pytest tests/macd2/test_zz_prod.py -q -p no:cacheprovider --basetemp=C:/Users/FURSYS/AppData/Local/Temp/claude/C--Users-FURSYS-Desktop-AI-GAP-2/4c76d5c5-d1f9-46cd-b140-2acac83bada0/scratchpad/rp/bt/${tag}_${D} > C:/Users/FURSYS/AppData/Local/Temp/claude/C--Users-FURSYS-Desktop-AI-GAP-2/4c76d5c5-d1f9-46cd-b140-2acac83bada0/scratchpad/rp/log/${tag}_${D}.txt 2>&1 && mv $out.tmp $out
