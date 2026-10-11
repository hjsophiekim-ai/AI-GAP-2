#!/bin/bash
W="C:/Users/KIMHYU~1/AppData/Local/Temp/claude/G----------------2--Desktop-AI-GAP-2/525f5404-4334-417e-b14f-ea2396b4f01f/scratchpad/dayreg"
cat "$W/jobs21.txt" | xargs -P "${1:-3}" -I{} bash -c 'set -- {}; "'"$W"'/job21.sh" "$1" "$2" "$3" "$4" "$5"'
echo "ALL DONE"
