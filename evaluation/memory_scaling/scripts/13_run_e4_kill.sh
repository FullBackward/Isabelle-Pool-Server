#!/usr/bin/env bash
# E4: with k loaded sessions, kill the newest session's prover; do the others keep answering? One row per tool.
set -u
HERE="$(cd "$(dirname "$0")/.." && pwd)"; cd "$HERE"
for tool in ips pide-a pide-b; do
  python -u -m memscale.run_experiment --exp kill --tool "$tool" --heap "${HEAP:-HOL}" --k "${K:-8}" --out "${OUT:-$HERE/results}" "$@"
done
