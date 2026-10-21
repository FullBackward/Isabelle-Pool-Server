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
LIMIT="${LIMIT:-48G}"; HEAP="${HEAP:-HOL-Analysis}"; OUT="${OUT:-$HERE/results}"; TOOLS="${TOOLS:-ips,pide-b}"
command -v systemd-run >/dev/null || { echo "systemd-run required (cgroup v2 scope with MemoryMax)"; exit 2; }
# After a kernel OOM kill inside the scope, systemd STOPS THE WHOLE SCOPE (OOMPolicy=stop by default):
# the experiment process was SIGTERMed with its victim (server run 2026-10-21: "Terminated", no result).
# OOMPolicy=continue leaves the scope running so the experiment observes the kill; scopes take it
# from systemd 253 on. Older: the run ends at the first kill and the manifest says `terminated`.
SD_VER="$(systemd-run --version 2>/dev/null | awk 'NR==1{print $2}')"
OOM_PROP=(-p OOMPolicy=continue)
if ! [ "${SD_VER:-0}" -ge 253 ] 2>/dev/null; then
  OOM_PROP=(); echo "WARNING: systemd ${SD_VER:-?} < 253: no OOMPolicy=continue on scopes; the PIDE B run ends at the first OOM kill"
fi
case ",$TOOLS," in *,ips,*)
  echo "== E3 IPS (scope MemoryMax=$LIMIT) $(date -u +%H:%M:%SZ)"
  "$PY" -u -m memscale.run_experiment --exp pressure --tool ips --heap "$HEAP" --memory-max "$LIMIT" --out "$OUT" "$@"
  ;;
esac
case ",$TOOLS," in *,pide-b,*)
  echo "== E3 PIDE B (experiment under a scope with MemoryMax=$LIMIT, ${OOM_PROP[*]:-no OOMPolicy}) $(date -u +%H:%M:%SZ)"
  systemd-run --user --scope --quiet -p "MemoryMax=$LIMIT" "${OOM_PROP[@]}" \
    "$PY" -u -m memscale.run_experiment --exp pressure --tool pide-b --heap "$HEAP" --out "$OUT" "$@"
  ;;
esac
