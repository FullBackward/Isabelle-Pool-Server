#!/usr/bin/env bash
# Build the heaps of config.yaml once (HOL ~ minutes, HOL-Library ~ 10-20 min, HOL-Analysis ~ 30-60 min
# on a large box). Both tools read the same heaps directory. Records the build times.
set -u
HERE="$(cd "$(dirname "$0")/.." && pwd)"
OUT="$HERE/results"; mkdir -p "$OUT"
HEAPS=${*:-$(grep -E '^\s{2}[A-Za-z-]+:\s*\{ks' "$HERE/config.yaml" | sed -E 's/^\s*([A-Za-z-]+):.*/\1/')}
for h in $HEAPS; do
  t0=$(date +%s); echo "== isabelle build -b $h ($(date -u +%H:%M:%SZ))"
  isabelle build -b "$h" 2>&1 | tail -3
  echo "$h $(( $(date +%s) - t0 ))s $(date -u +%Y-%m-%dT%H:%M:%SZ)" >> "$OUT/heap_build_times.txt"
done
cat "$OUT/heap_build_times.txt"
