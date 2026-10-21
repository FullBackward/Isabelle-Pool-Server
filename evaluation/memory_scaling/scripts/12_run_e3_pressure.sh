#!/usr/bin/env bash
# E3: memory pressure under a cgroup limit (default 48G) on one heap (default HOL-Analysis).
#   IPS: the server runs in its own scope with MemoryMax (the gate sees it, 62c7262); the runner is outside it.
#   PIDE B: the whole experiment runs inside a scope with MemoryMax (the MCP processes are its children).
set -u
HERE="$(cd "$(dirname "$0")/.." && pwd)"; cd "$HERE"
# python: the repo's .venv when it exists, else python3 (a bare Ubuntu has no `python`);
# an active virtualenv or PYTHON=... in the environment wins
PY="${PYTHON:-}"
[ -z "$PY" ] && [ -n "${VIRTUAL_ENV:-}" ] && PY="$VIRTUAL_ENV/bin/python"
[ -z "$PY" ] && [ -x "$HERE/../../.venv/bin/python" ] && PY="$HERE/../../.venv/bin/python"
[ -z "$PY" ] && PY="$(command -v python3 || command -v python)"
LIMIT="${LIMIT:-48G}"; HEAP="${HEAP:-HOL-Analysis}"; OUT="${OUT:-$HERE/results}"
command -v systemd-run >/dev/null || { echo "systemd-run required (cgroup v2 scope with MemoryMax)"; exit 2; }
echo "== E3 IPS (scope MemoryMax=$LIMIT) $(date -u +%H:%M:%SZ)"
"$PY" -u -m memscale.run_experiment --exp pressure --tool ips --heap "$HEAP" --memory-max "$LIMIT" --out "$OUT" "$@"
echo "== E3 PIDE B (experiment under a scope with MemoryMax=$LIMIT) $(date -u +%H:%M:%SZ)"
systemd-run --user --scope --quiet -p "MemoryMax=$LIMIT" \
  "$PY" -u -m memscale.run_experiment --exp pressure --tool pide-b --heap "$HEAP" --out "$OUT" "$@"
