#!/bin/bash
SP="C:/Users/FURSYS/AppData/Local/Temp/claude/C--Users-FURSYS-Desktop-AI-GAP-2/d13aa746-9cb3-4679-8340-b6996ac3502c/scratchpad"
R="$SP/wt"; S="$R/research_20261004_chop_staged"; W="$SP/pl"
VAR=$1; D=$2; P=$3; P2=$4
OUT="$W/out/REAL_${VAR}_${D}.json"
[ -f "$OUT" ] && { echo "skip $VAR $D"; exit 0; }
mkdir -p "$W/lock"; mkdir "$W/lock/${VAR}_${D}" 2>/dev/null || { echo "locked $VAR $D"; exit 0; }
TB="$W/bt/${VAR}_${D}"; rm -rf "$TB"
cd "$R"
PYTHONDONTWRITEBYTECODE=1 PYTHONIOENCODING=utf-8 WK_FILL=REAL WK_VAR=$VAR WK_OUT="$OUT" REPLAY_SP="$S/wk" WK_D=$D WK_P=$P WK_P2=$P2 \
  RS_TABLE="$S/rs_table.json" \
  python -m pytest tests/macd2/test_zz_wk22.py -q -p no:cacheprovider -x --basetemp="$TB" > "$W/out/log_${VAR}_${D}.txt" 2>&1
RC=$?
rm -rf "$TB"
echo "$VAR $D rc=$RC $(tail -1 "$W/out/log_${VAR}_${D}.txt")"
