#!/usr/bin/env bash
#
# Native (no Docker) one-shot setup for the Isabelle Pool Server: Python env,
# Isabelle component registration, ML heap cap, .env. Run from the repo root
# AFTER Isabelle is installed and `isabelle` is on PATH (or ISABELLE_HOME is
# set) — README "B. Native Linux server setup", steps B1-B2.
#
#   ./deploy/native_setup.sh              # venv + deps + component init + settings + .env
#   ./deploy/native_setup.sh --no-venv    # skip the venv/pip step (use the active interpreter)
#   ./deploy/native_setup.sh --verify     # ...then start the server, wait for /healthz,
#                                         #    prove `lemma True by simp`, stop it again
#
# Idempotent: re-run after moving the repo or upgrading Isabelle. Does NOT run
# the Gradle build (server/repl, README B5) — do that step separately.

set -euo pipefail
cd "$(dirname "$0")/.."
REPO="$PWD"

DO_VENV=1
DO_VERIFY=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --no-venv) DO_VENV=0; shift ;;
    --verify) DO_VERIFY=1; shift ;;
    -h|--help) sed -n '2,14p' "$0"; exit 0 ;;
    *) echo "unknown flag: $1" >&2; exit 2 ;;
  esac
done

echo "==> checking prerequisites"
if [[ -z "${ISABELLE_HOME:-}" ]]; then
  command -v isabelle >/dev/null || { echo "isabelle not on PATH and ISABELLE_HOME unset — install Isabelle first (README B2)" >&2; exit 1; }
  ISABELLE_HOME="$(isabelle getenv -b ISABELLE_HOME)"
  export ISABELLE_HOME
fi
[[ -x "$ISABELLE_HOME/bin/isabelle" ]] || { echo "no isabelle launcher under ISABELLE_HOME=$ISABELLE_HOME" >&2; exit 1; }
export PATH="$ISABELLE_HOME/bin:$PATH"
echo "    isabelle: $ISABELLE_HOME ($(isabelle getenv -b ISABELLE_IDENTIFIER))"
if [[ -n "${JAVA_HOME:-}" ]]; then
  echo "    WARNING: JAVA_HOME is exported ($JAVA_HOME). Isabelle 2026 resolves its own JDK and"
  echo "             fails with 'Unknown JAVA_HOME' under a foreign one — unset it for the server."
fi
command -v fc-match >/dev/null || echo "    WARNING: fontconfig missing — Isabelle's JVM needs it (README B1: fontconfig fonts-dejavu-core)"
PYTHON="$(command -v python3 || command -v python || true)"
[[ -n "$PYTHON" ]] || { echo "python3 not found" >&2; exit 1; }

if [[ "$DO_VENV" -eq 1 ]]; then
  echo "==> python env (.venv): server + mcp extra + client"
  [[ -d .venv ]] || "$PYTHON" -m venv .venv
  PYTHON="$REPO/.venv/bin/python"
  "$PYTHON" -m pip install --quiet --upgrade pip
  "$PYTHON" -m pip install --quiet -e ".[mcp]" -e ./client
fi

echo "==> registering the REPL component with Isabelle (py4j/spliff contribs)"
./server/repl/Admin/init

echo "==> ML heap cap + JVM GC logging in the Isabelle user settings"
./server/repl/Admin/ensure_settings.sh

echo "==> configuring .env"
if [[ ! -f .env ]]; then
  cp .env.example .env
  TOKEN="$("$PYTHON" -c 'import secrets; print(secrets.token_hex(16))')"
  sed -i "s/^ISABELLE_ADMIN_TOKEN=.*/ISABELLE_ADMIN_TOKEN=$TOKEN/" .env
  cat >> .env <<EOT

# --- native-host overrides (deploy/native_setup.sh; the defaults above are the
# --- container's /app and /root paths). Later lines win.
ISABELLE_HEAP_POOL_DIR=$HOME/.isabelle/heap_pool
ISABELLE_HEAP_POOL_ALLOWED_ROOTS=$REPO:$HOME/.isabelle
ISABELLE_SERVER_LOG_DIR=$REPO/logs
EOT
  echo "    created .env from .env.example (admin token generated, native path overrides appended)"
else
  echo "    .env already exists, keeping it"
fi
mkdir -p logs

run_server() {  # foreground: env from .env, venv python if present
  set -a; source .env; set +a
  exec "$PYTHON" -m server.app.main
}

if [[ "$DO_VERIFY" -eq 1 ]]; then
  PORT="$(grep -E '^ISABELLE_SERVER_PORT=' .env | tail -1 | cut -d= -f2)"; PORT="${PORT:-8000}"
  echo "==> smoke test: starting the server on :$PORT (log: logs/native_setup_verify.log)"
  ( run_server ) > logs/native_setup_verify.log 2>&1 &
  SRV=$!
  trap 'kill "$SRV" 2>/dev/null || true' EXIT
  for i in $(seq 1 60); do
    curl -fsS -m 3 "http://localhost:${PORT}/healthz" >/dev/null 2>&1 && break
    kill -0 "$SRV" 2>/dev/null || { echo "server exited early — see logs/native_setup_verify.log" >&2; exit 1; }
    [[ "$i" -eq 60 ]] && { echo "server not healthy after 5 min — see logs/native_setup_verify.log" >&2; exit 1; }
    sleep 5
  done
  RESP="$(curl -fsS -m 600 -X POST "http://localhost:${PORT}/api/v1/sessions/acquire" \
    -H 'Content-Type: application/json' -d '{"theories": ["Main"], "field": "HOL"}')"
  SID="$(echo "$RESP" | "$PYTHON" -c 'import json,sys; print(json.load(sys.stdin)["session_id"])')"
  LEASE="$(echo "$RESP" | "$PYTHON" -c 'import json,sys; print(json.load(sys.stdin)["lease_id"])')"
  # a fresh session has no theory begun — enter one (header generated server-side)
  curl -fsS -m 300 -X POST "http://localhost:${PORT}/api/v1/sessions/$SID/enter_theory/Scratch" \
    -H 'Content-Type: application/json' -H "X-Lease-Id: $LEASE" -d '{"imports": ["Main"]}' >/dev/null
  curl -fsS -m 120 -X POST "http://localhost:${PORT}/api/v1/sessions/$SID/verify_chunk" \
    -H 'Content-Type: application/json' -H "X-Lease-Id: $LEASE" \
    -d '{"chunk": "lemma True by simp", "timeout": 60}' | "$PYTHON" -m json.tool
  curl -fsS -m 30 -X POST "http://localhost:${PORT}/api/v1/sessions/$SID/release" -H "X-Lease-Id: $LEASE" >/dev/null || true
  kill "$SRV" 2>/dev/null || true; wait "$SRV" 2>/dev/null || true
  trap - EXIT
  echo "    smoke test done (server stopped)"
fi

cat <<EOT

Native setup done. Run the server (foreground):
  set -a; source .env; set +a
  ${PYTHON} -m server.app.main
  curl http://localhost:8000/healthz        # 1-2 min for the gateway JVM
Run on boot: systemd unit in README B7. Prebuild heaps: isabelle build -b HOL-Library
EOT
