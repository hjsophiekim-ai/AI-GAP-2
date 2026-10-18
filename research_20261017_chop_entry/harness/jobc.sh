#!/bin/bash
# $1=TAG $2=D $3=P $4=P2 ; env per TAG
SP="C:/Users/KIMHYU~1/AppData/Local/Temp/claude/G----------------2--Desktop-AI-GAP-2/4745b385-5441-4c15-97e7-446b1c980929/scratchpad"
W="C:/Users/KIMHYU~1/AppData/Local/Temp/claude/G----------------2--Desktop-AI-GAP-2/e202387f-ec1a-41fa-a078-e46b72ece959/scratchpad/wk"
TAG=$1; D=$2; P=$3; P2=$4
OUT="$W/out/${TAG}_${D}.json"
[ -f "$OUT" ] && { echo "skip $TAG $D"; exit 0; }
case "$TAG" in
  CHOP)      XE=(CHOPMODE=1 FEATLOG=1);;
  CPAR)      XE=(FEATLOG=1);;
  CEMAOFF)   XE=(CHOPMODE=1 EMAV=OFF);;
  CEMASOFT)  XE=(CHOPMODE=1 EMAV=SOFT);;
  CEMASOFT) XE=(CHOPMODE=1 EMAV=SOFT);;
  CPE05)    XE=(CHOPMODE=1 PE60MAX=0.05);;
  CPE10)    XE=(CHOPMODE=1 PE60MAX=0.10);;
  CEMAPE)   XE=(CHOPMODE=1 EMAV=SOFT PE60MAX=0.05);;
  EEMAOFF)   XE=(EMAV=OFF);;
  EEMASOFT)  XE=(EMAV=SOFT);;
  *) echo "unknown tag $TAG"; exit 2;;
esac
TB="$W/btc/${TAG}_${D}"; rm -rf "$TB"; mkdir -p "$W/btc" "$W/out"
cd "$SP/wt"
env PYTHONDONTWRITEBYTECODE=1 PYTHONIOENCODING=utf-8 WK_FILL=REAL WK_VAR=UPFASTRS WK_OUT="$OUT" \
  REPLAY_SP="$SP/bb/sp" WK_D=$D WK_P=$P WK_P2=$P2 RS_TABLE="$SP/bb/rs_table.json" \
  MACD2_SLOW_TP2_ENABLED=true "${XE[@]}" \
  python -m pytest tests/macd2/test_zz_v3.py -q -p no:cacheprovider -x --basetemp="$TB" > "$W/out/log_${TAG}_${D}.txt" 2>&1
RC=$?
rm -rf "$TB"
echo "$TAG $D rc=$RC $(tail -1 "$W/out/log_${TAG}_${D}.txt")"
