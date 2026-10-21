#!/usr/bin/env bash
# Tables, plots and the report snippet from results/ (rerun any time; reads every cell with a manifest).
set -u
HERE="$(cd "$(dirname "$0")/.." && pwd)"
# python: the repo's .venv when it exists, else python3 (a bare Ubuntu has no `python`);
# an active virtualenv or PYTHON=... in the environment wins
PY="${PYTHON:-}"
[ -z "$PY" ] && [ -n "${VIRTUAL_ENV:-}" ] && PY="$VIRTUAL_ENV/bin/python"
[ -z "$PY" ] && [ -x "$HERE/../../.venv/bin/python" ] && PY="$HERE/../../.venv/bin/python"
[ -z "$PY" ] && PY="$(command -v python3 || command -v python)"
cd "$HERE"
exec "$PY" -m memscale.analyze --results "${RESULTS:-$HERE/results}" "$@"
