#!/bin/bash
# job2.sh S FILL VAR D P P2
S="$1"; FILL=$2; VAR=$3; D=$4; P=$5; P2=$6; cd "C:/Users/FURSYS/Desktop/AI-GAP 2"
case "$VAR" in C0|D0) echo "drop $FILL $VAR $D"; exit 0;; esac
OUT="$S/wk/out2/${FILL}_${VAR}_${D}.json"
[ -f "$OUT" ] && { echo "skip $FILL $VAR $D"; exit 0; }
PYTHONDONTWRITEBYTECODE=1 WK_FILL=$FILL WK_VAR=$VAR WK_OUT="$OUT" REPLAY_SP="$S/wk" WK_D=$D WK_P=$P WK_P2=$P2 \
  python -m pytest tests/macd2/test_zz_wk2.py -q -p no:cacheprovider -x > "$S/wk/out2/log_${FILL}_${VAR}_${D}.txt" 2>&1
echo "$FILL $VAR $D $(tail -1 $S/wk/out2/log_${FILL}_${VAR}_${D}.txt)"
