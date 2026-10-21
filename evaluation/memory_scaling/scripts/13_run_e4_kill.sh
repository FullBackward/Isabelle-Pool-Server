#!/usr/bin/env bash
# E4: with k loaded sessions, kill the newest session's prover; do the others keep answering? One row per tool.
set -u
HERE="$(cd "$(dirname "$0")/.." && pwd)"; cd "$HERE"
# python: the repo's .venv when it exists, else python3 (a bare Ubuntu has no `python`);
# an active virtualenv or PYTHON=... in the environment wins
PY="${PYTHON:-}"
[ -z "$PY" ] && [ -n "${VIRTUAL_ENV:-}" ] && PY="$VIRTUAL_ENV/bin/python"
[ -z "$PY" ] && [ -x "$HERE/../../.venv/bin/python" ] && PY="$HERE/../../.venv/bin/python"
[ -z "$PY" ] && PY="$(command -v python3 || command -v python)"
for tool in ips pide-a pide-b; do
  "$PY" -u -m memscale.run_experiment --exp kill --tool "$tool" --heap "${HEAP:-HOL}" --k "${K:-8}" --out "${OUT:-$HERE/results}" "$@"
done
