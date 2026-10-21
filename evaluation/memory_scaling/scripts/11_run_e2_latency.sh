#!/usr/bin/env bash
# E2: warm-hit latency on IPS (release + re-acquire with the same imports), per heap.
set -u
HERE="$(cd "$(dirname "$0")/.." && pwd)"; cd "$HERE"
# python: the repo's .venv when it exists, else python3 (a bare Ubuntu has no `python`);
# an active virtualenv or PYTHON=... in the environment wins
PY="${PYTHON:-}"
[ -z "$PY" ] && [ -n "${VIRTUAL_ENV:-}" ] && PY="$VIRTUAL_ENV/bin/python"
[ -z "$PY" ] && [ -x "$HERE/../../.venv/bin/python" ] && PY="$HERE/../../.venv/bin/python"
[ -z "$PY" ] && PY="$(command -v python3 || command -v python)"
for h in ${HEAPS:-HOL HOL-Library HOL-Analysis}; do
  "$PY" -u -m memscale.run_experiment --exp warm --tool ips --heap "$h" --k "${K:-4}" --rounds "${ROUNDS:-3}" --out "${OUT:-$HERE/results}" "$@"
done
