#!/bin/bash
# run6.sh <parallel>
S="G:/다른 컴퓨터/내 노트북 (2)/Desktop/AI-GAP 2/research_20261003_breakout_confirm"
N=${1:-3}
cat "$S/jobs6.txt" | xargs -P "$N" -I{} bash -c 'set -- {}; "'"$S"'/job6.sh" "$1" "$2" "$3" "$4" "$5"'
echo "ALL DONE"
