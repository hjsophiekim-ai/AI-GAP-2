#!/bin/bash
S="G:/다른 컴퓨터/내 노트북 (2)/Desktop/AI-GAP 2/research_20261004_chop_staged"
R="G:/다른 컴퓨터/내 노트북 (2)/Desktop/AI-GAP 2"
TB="C:/Users/KIMHYU~1/AppData/Local/Temp/claude/G----------------2--Desktop-AI-GAP-2/3fbe827e-5426-493f-b58f-3eea8fdb32c5/scratchpad/bt8"
FILL=$1; VAR=$2; D=$3; P=$4; P2=$5
OUT="$S/wk/out8/${FILL}_${VAR}_${D}.json"
[ -f "$OUT" ] && { echo "skip $FILL $VAR $D"; exit 0; }
mkdir -p "$TB"
cd "$R"
PYTHONDONTWRITEBYTECODE=1 WK_FILL=$FILL WK_VAR=$VAR WK_OUT="$OUT" REPLAY_SP="$S/wk" WK_D=$D WK_P=$P WK_P2=$P2 \
  python -m pytest tests/macd2/test_zz_wk8.py -q -p no:cacheprovider -x --basetemp="$TB/${VAR}_${D}" > "$S/wk/out8/log_${FILL}_${VAR}_${D}.txt" 2>&1
RC=$?
rm -rf "$TB/${VAR}_${D}"
echo "$FILL $VAR $D rc=$RC $(tail -1 "$S/wk/out8/log_${FILL}_${VAR}_${D}.txt")"
