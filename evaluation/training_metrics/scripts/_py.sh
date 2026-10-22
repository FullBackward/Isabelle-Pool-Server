# Sourced by the scripts: pick the Python (an active virtualenv, the repo's .venv, else python3)
# and put the package on PYTHONPATH. PYTHON=... in the environment wins.
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
PY="${PYTHON:-}"
[ -z "$PY" ] && [ -n "${VIRTUAL_ENV:-}" ] && PY="$VIRTUAL_ENV/bin/python"
[ -z "$PY" ] && [ -x "$REPO/.venv/bin/python" ] && PY="$REPO/.venv/bin/python"
[ -z "$PY" ] && PY="$(command -v python3 || command -v python)"
export PYTHONPATH="$HERE:$REPO:$REPO/evaluation/memory_scaling${PYTHONPATH:+:$PYTHONPATH}"
cd "$HERE"
