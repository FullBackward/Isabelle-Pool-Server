#!/usr/bin/env bash
# E2: warm-hit latency on IPS (release + re-acquire with the same imports), per heap.
set -u
HERE="$(cd "$(dirname "$0")/.." && pwd)"; cd "$HERE"
for h in ${HEAPS:-HOL HOL-Library HOL-Analysis}; do
  python -u -m memscale.run_experiment --exp warm --tool ips --heap "$h" --k "${K:-4}" --rounds "${ROUNDS:-3}" --out "${OUT:-$HERE/results}" "$@"
done
