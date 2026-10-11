#!/bin/sh
# THROWAWAY (Phase 5 spike). Three consecutive full-suite runs, wall clock + JUnit XML each.
# usage: q1_full_runs.sh SPIKE_VENV OUT_DIR
SV="$1"; OUT="$2"; mkdir -p "$OUT"
cd "$(dirname "$0")/../.." || exit 1
for i in 1 2 3; do
  s=$(python3 -c 'import time; print(time.time())')
  "$SV/bin/python" -m pytest tests -q -p no:cacheprovider --junitxml="$OUT/run$i.xml" > "$OUT/run$i.log" 2>&1
  e=$(python3 -c 'import time; print(time.time())')
  python3 -c "print('run $i', round($e - $s, 1), 's')" >> "$OUT/times.txt"
done
