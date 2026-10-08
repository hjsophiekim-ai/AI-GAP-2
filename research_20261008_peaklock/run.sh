#!/bin/bash
W="C:/Users/FURSYS/AppData/Local/Temp/claude/C--Users-FURSYS-Desktop-AI-GAP-2/d13aa746-9cb3-4679-8340-b6996ac3502c/scratchpad/pl"
cat "$W/$1" | xargs -P "${2:-5}" -I{} bash -c 'set -- {}; "'"$W"'/job.sh" "$1" "$2" "$3" "$4"'
echo "ALL DONE $1"
