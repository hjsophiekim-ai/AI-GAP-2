#!/bin/bash
R="G:/다른 컴퓨터/내 노트북 (2)/Desktop/AI-GAP 2"
S="$R/research_20261004_chop_staged"
W="C:/Users/KIMHYU~1/AppData/Local/Temp/claude/G----------------2--Desktop-AI-GAP-2/525f5404-4334-417e-b14f-ea2396b4f01f/scratchpad/dayreg"
TB="C:/Users/KIMHYU~1/AppData/Local/Temp/claude/G----------------2--Desktop-AI-GAP-2/525f5404-4334-417e-b14f-ea2396b4f01f/scratchpad/bt21"
FILL=$1; VAR=$2; D=$3; P=$4; P2=$5
OUT="$W/wk21/${FILL}_${VAR}_${D}.json"
[ -f "$OUT" ] && { echo "skip $FILL $VAR $D"; exit 0; }
rm -rf "$TB/${VAR}_${D}"; mkdir -p "$TB" "$W/wk21"
cd "$R"
PYTHONDONTWRITEBYTECODE=1 WK_FILL=$FILL WK_VAR=$VAR WK_OUT="$OUT" REPLAY_SP="$S/wk" WK_D=$D WK_P=$P WK_P2=$P2 \
  RS_TABLE="$S/rs_table.json" \
  python -m pytest tests/macd2/test_zz_wk21.py -q -p no:cacheprovider -x --basetemp="$TB/${VAR}_${D}" > "$W/wk21/log_${FILL}_${VAR}_${D}.txt" 2>&1
RC=$?
rm -rf "$TB/${VAR}_${D}"
echo "$FILL $VAR $D rc=$RC $(tail -1 "$W/wk21/log_${FILL}_${VAR}_${D}.txt")"
