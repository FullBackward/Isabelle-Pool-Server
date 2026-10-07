#!/usr/bin/env bash
#
# Isabelle Pool Server one-shot setup: configure .env, build the image, start the
# server, wait for health. Run from the repo root (Linux/macOS/WSL/Git Bash):
#
#   ./deploy/setup.sh                          # configure + build + start + health-wait
#   ./deploy/setup.sh --verify                 # ...plus a smoke test (acquire + prove "lemma True by simp")
#   ./deploy/setup.sh --build-heaps "HOL-Library HOL-Analysis"   # ...plus prebuild session heaps
#   ./deploy/setup.sh --no-build               # skip the image build (fast restart)
#
# Heaps are built INSIDE the running container on the idle stack and persist
# in the isabelle_user_data volume (they survive restarts, not volume deletion).

set -euo pipefail
# lives in deploy/; everything below runs from the repo root (compose, .env)
cd "$(dirname "$0")/.."

PORT="${ISABELLE_SERVER_PORT:-8000}"

# JSON helper: host python3, else python, else the container's own interpreter
# (Windows/Git Bash hosts often have neither on PATH).
py() {
  if command -v python3 >/dev/null 2>&1; then python3 "$@"
  elif command -v python >/dev/null 2>&1; then python "$@"
  else docker compose exec -T isabelle-pool-server python "$@"
  fi
}
DO_BUILD=1
DO_VERIFY=0
HEAPS=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --no-build) DO_BUILD=0; shift ;;
    --verify) DO_VERIFY=1; shift ;;
    --build-heaps) HEAPS="${2:?--build-heaps needs a quoted list}"; shift 2 ;;
    -h|--help) sed -n '2,12p' "$0"; exit 0 ;;
    *) echo "unknown flag: $1" >&2; exit 2 ;;
  esac
done

echo "==> checking docker"
command -v docker >/dev/null || { echo "docker not found in PATH" >&2; exit 1; }
docker compose version >/dev/null 2>&1 || { echo "'docker compose' (v2) not available" >&2; exit 1; }
docker info >/dev/null 2>&1 || { echo "docker daemon not running (start Docker Desktop / the docker service)" >&2; exit 1; }

echo "==> configuring .env"
if [[ ! -f .env ]]; then
  cp .env.example .env
  TOKEN="$(python3 -c 'import secrets; print(secrets.token_hex(16))' 2>/dev/null \
        || openssl rand -hex 16 2>/dev/null \
        || head -c 16 /dev/urandom | od -An -tx1 | tr -d ' \n')"
  if [[ -n "$TOKEN" ]]; then
    sed -i "s/^ISABELLE_ADMIN_TOKEN=.*/ISABELLE_ADMIN_TOKEN=$TOKEN/" .env
    echo "    created .env from .env.example (admin token generated)"
  else
    echo "    created .env from .env.example (WARNING: could not generate an admin token;"
    echo "    set ISABELLE_ADMIN_TOKEN in .env manually to enable the admin console)"
  fi
else
  echo "    .env already exists, keeping it"
fi

if [[ "$DO_BUILD" -eq 1 ]]; then
  echo "==> building image (first build downloads Isabelle — long)"
  docker compose build isabelle-pool-server
fi

echo "==> starting server"
docker compose up -d isabelle-pool-server

echo "==> waiting for healthz on port $PORT"
for i in $(seq 1 90); do
  if curl -fsS -m 3 "http://localhost:${PORT}/healthz" >/dev/null 2>&1; then
    break
  fi
  if [[ "$i" -eq 90 ]]; then
    echo "server did not become healthy in ~7.5 min; check: docker compose logs isabelle-pool-server" >&2
    exit 1
  fi
  sleep 5
done
echo "    healthy: $(curl -fsS -m 5 "http://localhost:${PORT}/healthz")"
curl -fsS -m 5 "http://localhost:${PORT}/" | py -m json.tool 2>/dev/null || true

if [[ -n "$HEAPS" ]]; then
  for heap in $HEAPS; do
    echo "==> building heap: $heap (long for Analysis-scale; safe to re-run)"
    docker compose exec isabelle-pool-server isabelle build -b "$heap"
  done
  echo "==> heaps now available:"
  curl -fsS -m 5 "http://localhost:${PORT}/api/v1/heaps/available" | py -m json.tool 2>/dev/null || true
fi

if [[ "$DO_VERIFY" -eq 1 ]]; then
  echo "==> smoke test: acquire + enter theory + verify 'lemma True by simp'"
  RESP="$(curl -fsS -m 300 -X POST "http://localhost:${PORT}/api/v1/sessions/acquire" \
    -H 'Content-Type: application/json' -d '{"theories": ["Main"], "field": "HOL"}')"
  SID="$(echo "$RESP" | py -c 'import json,sys; print(json.load(sys.stdin)["session_id"])')"
  LEASE="$(echo "$RESP" | py -c 'import json,sys; print(json.load(sys.stdin)["lease_id"])')"
  echo "    session: $SID"
  # a fresh session has no theory begun — enter one (header generated server-side)
  curl -fsS -m 300 -X POST "http://localhost:${PORT}/api/v1/sessions/$SID/enter_theory/Scratch" \
    -H 'Content-Type: application/json' -H "X-Lease-Id: $LEASE" -d '{"imports": ["Main"]}' >/dev/null
  curl -fsS -m 120 -X POST "http://localhost:${PORT}/api/v1/sessions/$SID/verify_chunk" \
    -H 'Content-Type: application/json' -H "X-Lease-Id: $LEASE" \
    -d '{"chunk": "lemma True by simp", "timeout": 60}' | py -m json.tool
  curl -fsS -m 30 -X POST "http://localhost:${PORT}/api/v1/sessions/$SID/release" \
    -H "X-Lease-Id: $LEASE" >/dev/null || true
  echo "    smoke test done (session released)"
fi

echo
echo "Isabelle Pool Server is up:"
echo "  API:          http://localhost:${PORT}/  (healthz: /healthz)"
echo "  Admin console: http://localhost:${PORT}/admin"
echo "  Live logs:    docker compose logs -f isabelle-pool-server   (or: tail -f logs/server.log)"
echo "  Stop:         docker compose stop isabelle-pool-server"
