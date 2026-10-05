#!/bin/bash
S="G:/다른 컴퓨터/내 노트북 (2)/Desktop/AI-GAP 2/research_20261004_chop_staged"
cat "$S/jobs17.txt" | xargs -P "${1:-4}" -I{} bash -c 'set -- {}; "'"$S"'/job17.sh" "$1" "$2" "$3" "$4" "$5"'
echo "ALL DONE"
