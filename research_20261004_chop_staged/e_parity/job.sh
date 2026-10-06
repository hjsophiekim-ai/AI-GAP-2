#!/bin/bash
W="/c/Users/FURSYS/AppData/Local/Temp/claude/C--Users-FURSYS-Desktop-AI-GAP-2/ab9b022b-2c5e-45ea-83df-2a565ee8c06e/scratchpad/wt_e"; S="/c/Users/FURSYS/AppData/Local/Temp/claude/C--Users-FURSYS-Desktop-AI-GAP-2/ab9b022b-2c5e-45ea-83df-2a565ee8c06e/scratchpad/wt_reftp/research_20261004_chop_staged"; TB="/c/Users/FURSYS/AppData/Local/Temp/claude/C--Users-FURSYS-Desktop-AI-GAP-2/ab9b022b-2c5e-45ea-83df-2a565ee8c06e/scratchpad/eprod/bt_final"; OD="/c/Users/FURSYS/AppData/Local/Temp/claude/C--Users-FURSYS-Desktop-AI-GAP-2/ab9b022b-2c5e-45ea-83df-2a565ee8c06e/scratchpad/eprod/out_final"
VAR=$1; D=$2; P=$3; P2=$4
OUT="$OD/REAL_${VAR}_${D}.json"
[ -f "$OUT" ] && { echo "skip $VAR $D"; exit 0; }
mkdir -p "$TB" "$OD"; rm -rf "$TB/${VAR}_${D}"
cd "$W"
PYTHONDONTWRITEBYTECODE=1 PYTHONIOENCODING=utf-8 WK_FILL=REAL WK_VAR=$VAR WK_OUT="$OUT" REPLAY_SP="$S/wk" WK_D=$D WK_P=$P WK_P2=$P2 RS_TABLE="$S/rs_table.json"   python -m pytest tests/macd2/test_zz_eprod.py -q -p no:cacheprovider -x --basetemp="$TB/${VAR}_${D}" > "$OD/log_${VAR}_${D}.txt" 2>&1
RC=$?; rm -rf "$TB/${VAR}_${D}"
echo "$VAR $D rc=$RC $(tail -1 "$OD/log_${VAR}_${D}.txt")"
