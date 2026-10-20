#!/usr/bin/env bash
# E1: the full capacity matrix (hours; resumable — rerun the same command after an interruption).
# Run under nohup/tmux:  nohup scripts/10_run_e1.sh > results/e1.log 2>&1 &
set -u
HERE="$(cd "$(dirname "$0")/.." && pwd)"
cd "$HERE"
exec python -u -m memscale.run_matrix --config "$HERE/config.yaml" --out "${OUT:-$HERE/results}" "$@"
