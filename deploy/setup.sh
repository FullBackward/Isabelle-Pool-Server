#!/usr/bin/env bash
#
# Isabelle Pool Server — Docker setup in one command. Run from the repo root
# (Linux/macOS/WSL/Git Bash):
#
#   ./deploy/setup.sh                   configure .env, build the image, start, wait for /healthz
#   ./deploy/setup.sh --verify          ... and prove `lemma True by simp` through the API
#   ./deploy/setup.sh --install-docker  ... first install Docker Engine + Compose (Ubuntu/Debian, sudo)
#   ./deploy/setup.sh --build-heaps "HOL-Library HOL-Analysis"   ... and prebuild session heaps
#   ./deploy/setup.sh --no-build        restart without rebuilding the image
#
# Everything noisy (docker build, heap builds) goes to logs/setup.log; the
# terminal shows one line per step. Heaps are built inside the running
# container and persist in the isabelle_user_data volume.

set -euo pipefail
cd "$(dirname "$0")/.."

LOG_FILE="logs/setup.log"
PY_FALLBACK="docker compose exec -T isabelle-pool-server python"
# shellcheck source=deploy/lib.sh
. "$(dirname "$0")/lib.sh"

PORT="${ISABELLE_SERVER_PORT:-8000}"
BASE="http://localhost:${PORT}"
DO_BUILD=1 DO_VERIFY=0 DO_INSTALL_DOCKER=0 HEAPS=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --no-build) DO_BUILD=0; shift ;;
    --verify) DO_VERIFY=1; shift ;;
    --install-docker) DO_INSTALL_DOCKER=1; shift ;;
    --build-heaps) HEAPS="${2:?--build-heaps needs a quoted list}"; shift 2 ;;
    -h|--help) sed -n '3,14p' "$0"; exit 0 ;;
    *) echo "unknown flag: $1 (see --help)" >&2; exit 2 ;;
  esac
done
log_init "$@"

if [[ "$DO_INSTALL_DOCKER" -eq 1 ]]; then
  step "installing Docker Engine + Compose (apt, sudo)"
  command -v apt-get >/dev/null || die "--install-docker supports apt-based systems only; see https://docs.docker.com/engine/install/"
  run_logged "apt prerequisites" sudo apt-get install -y git curl ca-certificates gnupg
  run_logged "docker apt repository" bash -c '
    set -e
    sudo install -m 0755 -d /etc/apt/keyrings
    . /etc/os-release
    curl -fsSL "https://download.docker.com/linux/${ID}/gpg" | sudo gpg --dearmor --yes -o /etc/apt/keyrings/docker.gpg
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/${ID} ${VERSION_CODENAME} stable" \
      | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
    sudo apt-get update'
  run_logged "docker packages" sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin
  run_logged "docker group for $USER" sudo usermod -aG docker "$USER"
  if ! docker info >/dev/null 2>&1; then
    note "Docker is installed. Your shell does not have the docker group yet:"
    note "log out and back in (or run: newgrp docker), then re-run ./deploy/setup.sh"
    exit 0
  fi
fi

step "checking docker"
command -v docker >/dev/null || die "docker not found — run ./deploy/setup.sh --install-docker (Ubuntu/Debian) or install Docker Desktop"
docker compose version >/dev/null 2>&1 || die "'docker compose' (v2) not available — install the docker-compose-plugin"
docker info >/dev/null 2>&1 || die "docker daemon not running (start Docker Desktop / the docker service)"
ok "$(docker --version | sed 's/,.*//')"

step "configuring .env"
if [[ ! -f .env ]]; then
  cp .env.example .env
  TOKEN="$(py -c 'import secrets; print(secrets.token_hex(16))' 2>/dev/null \
        || openssl rand -hex 16 2>/dev/null \
        || head -c 16 /dev/urandom | od -An -tx1 | tr -d ' \n')"
  if [[ -n "$TOKEN" ]]; then
    sed -i "s/^ISABELLE_ADMIN_TOKEN=.*/ISABELLE_ADMIN_TOKEN=$TOKEN/" .env
    ok "created .env from .env.example (admin token generated)"
  else
    ok "created .env from .env.example"
    note "WARNING: could not generate an admin token — set ISABELLE_ADMIN_TOKEN in .env to enable the admin console"
  fi
else
  ok ".env exists, keeping it"
fi

if [[ "$DO_BUILD" -eq 1 ]]; then
  step "building the image"
  note "first build downloads Isabelle (~1.2 GB) and compiles the backend: 10-30 min; later builds are cached"
  run_logged "docker compose build" docker compose build isabelle-pool-server
fi

step "starting the server"
run_logged "docker compose up -d" docker compose up -d isabelle-pool-server
wait_healthz "$BASE" 450 || die "server did not become healthy — check: docker compose logs isabelle-pool-server"

if [[ -n "$HEAPS" ]]; then
  step "prebuilding session heaps (long for Analysis-scale; safe to re-run)"
  for heap in $HEAPS; do
    run_logged "isabelle build -b $heap" docker compose exec -T isabelle-pool-server isabelle build -b "$heap"
  done
fi

if [[ "$DO_VERIFY" -eq 1 ]]; then
  step "smoke test"
  smoke_test "$BASE" || die "smoke test failed"
fi

cat <<EOT

Isabelle Pool Server is up.
  API:           $BASE/         (health: /healthz, docs: /docs)
  Admin console: $BASE/admin    (token: ISABELLE_ADMIN_TOKEN in .env)
  Logs:          docker compose logs -f isabelle-pool-server   (file: logs/server.log)
  Stop / start:  docker compose stop isabelle-pool-server  /  ./deploy/setup.sh --no-build
  Setup log:     $LOG_FILE
EOT
