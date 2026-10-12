#!/bin/bash
wt=$1; D=$2; P=$3; P2=$4
out=C:/Users/FURSYS/AppData/Local/Temp/claude/C--Users-FURSYS-Desktop-AI-GAP-2/4c76d5c5-d1f9-46cd-b140-2acac83bada0/scratchpad/n1/out/${wt}_${D}.json
[ -s "$out" ] && exit 0
cd C:/Users/FURSYS/AppData/Local/Temp/claude/C--Users-FURSYS-Desktop-AI-GAP-2/4c76d5c5-d1f9-46cd-b140-2acac83bada0/scratchpad/$wt
WK_D=$D WK_P=$P WK_P2=$P2 WK_FILL=REAL WK_VAR=B MACD2_SLOW_TP2_ENABLED=0 REPLAY_SP=C:/Users/FURSYS/AppData/Local/Temp/claude/C--Users-FURSYS-Desktop-AI-GAP-2/4c76d5c5-d1f9-46cd-b140-2acac83bada0/scratchpad/rp WK_OUT=$out.tmp PYTHONIOENCODING=utf-8 python -m pytest tests/macd2/test_zz_prod_orig.py -q -p no:cacheprovider --basetemp=C:/Users/FURSYS/AppData/Local/Temp/claude/C--Users-FURSYS-Desktop-AI-GAP-2/4c76d5c5-d1f9-46cd-b140-2acac83bada0/scratchpad/n1/bt/${wt}_${D} > C:/Users/FURSYS/AppData/Local/Temp/claude/C--Users-FURSYS-Desktop-AI-GAP-2/4c76d5c5-d1f9-46cd-b140-2acac83bada0/scratchpad/n1/log/${wt}_${D}.txt 2>&1 && mv $out.tmp $out
