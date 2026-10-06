#!/bin/bash
# usage: run_days.sh S TAG MODE "D:P:P2 D:P:P2 ..." [ENV...]
S="$1"; TAG="$2"; MODE="$3"; SPECS="$4"; ENVS="$5"; cd "C:/Users/FURSYS/Desktop/AI-GAP 2"
F=tests/macd2/test_zz_wkreplay_$TAG.py
cp "$S/wk/test_zz_wkreplay.py" $F
SFX=$([ "$MODE" = "MODE_N1" ] && echo _N1 || echo "")
[ -n "$ENVS" ] && SFX="$SFX$6"
for spec in $SPECS; do
  IFS=: read D P P2 <<< "$spec"
  env $ENVS WK_MODE=$MODE WK_SFX=$SFX REPLAY_SP="$S/wk" WK_D=$D WK_P=$P WK_P2=$P2 python -m pytest $F -q -p no:cacheprovider -x > "$S/wk/out/log${SFX}_$D.txt" 2>&1
  echo "$TAG $D $(tail -1 $S/wk/out/log${SFX}_$D.txt)"
done
rm -f $F
