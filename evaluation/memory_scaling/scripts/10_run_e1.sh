#!/usr/bin/env bash
# E1: the full capacity matrix (hours; resumable — rerun the same command after an interruption).
# Run under nohup/tmux:  nohup scripts/10_run_e1.sh > results/e1.log 2>&1 &
set -u
HERE="$(cd "$(dirname "$0")/.." && pwd)"
# python: the repo's .venv when it exists, else python3 (a bare Ubuntu has no `python`);
# an active virtualenv or PYTHON=... in the environment wins
PY="${PYTHON:-}"
[ -z "$PY" ] && [ -n "${VIRTUAL_ENV:-}" ] && PY="$VIRTUAL_ENV/bin/python"
[ -z "$PY" ] && [ -x "$HERE/../../.venv/bin/python" ] && PY="$HERE/../../.venv/bin/python"
[ -z "$PY" ] && PY="$(command -v python3 || command -v python)"
cd "$HERE"
exec "$PY" -u -m memscale.run_matrix --config "$HERE/config.yaml" --out "${OUT:-$HERE/results}" "$@"
