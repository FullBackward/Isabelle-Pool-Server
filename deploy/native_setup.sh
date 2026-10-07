#!/usr/bin/env bash
#
# Isabelle Pool Server — native (no Docker) setup in one command. Run from the
# repo root on Linux:
#
#   ./deploy/native_setup.sh --system --systemd --verify   # bare Ubuntu/Debian box -> running service
#
#   --system             apt packages (deploy/apt_packages.txt) + Isabelle download to
#                        /opt/isabelle (mirrors: deploy/isabelle_mirrors.txt) + profile line (sudo)
#   --isabelle-version V Isabelle distribution for --system (default: ISABELLE_VERSION in .env /
#                        .env.example, currently Isabelle2026-RC2)
#   --no-venv            use the active Python instead of creating .venv
#   --systemd            install and enable the systemd unit (deploy/isabelle-pool-server.service) (sudo)
#   --verify             start the server (or use the systemd one), prove `lemma True by simp`, stop it
#
# Without flags it does the user-level part only: .venv + deps, Isabelle
# component registration, Scala backend build, ML heap cap, .env. Idempotent:
# re-run after moving the repo or upgrading Isabelle. Noisy output goes to
# logs/native_setup.log; the terminal shows one line per step.

set -euo pipefail
cd "$(dirname "$0")/.."
REPO="$PWD"

LOG_FILE="logs/native_setup.log"
# shellcheck source=deploy/lib.sh
. "$(dirname "$0")/lib.sh"

DO_SYSTEM=0 DO_VENV=1 DO_SYSTEMD=0 DO_VERIFY=0
# Isabelle version: flag > environment > .env > .env.example (same key the
# docker-compose build arg reads, so both paths track one setting).
env_version() { grep -hE '^ISABELLE_VERSION=' "$@" 2>/dev/null | tail -1 | cut -d= -f2; }
ISABELLE_VERSION="${ISABELLE_VERSION:-$(env_version .env .env.example)}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --system) DO_SYSTEM=1; shift ;;
    --isabelle-version) ISABELLE_VERSION="${2:?--isabelle-version needs a value}"; shift 2 ;;
    --no-venv) DO_VENV=0; shift ;;
    --systemd) DO_SYSTEMD=1; shift ;;
    --verify) DO_VERIFY=1; shift ;;
    -h|--help) sed -n '3,20p' "$0"; exit 0 ;;
    *) echo "unknown flag: $1 (see --help)" >&2; exit 2 ;;
  esac
done
log_init "$@"
[[ -n "$ISABELLE_VERSION" ]] || die "ISABELLE_VERSION not set (expected in .env.example)"

# strip comments/blank lines from a list file
list_file() { sed -e 's/#.*//' -e '/^[[:space:]]*$/d' "$1"; }

# ---------------------------------------------------------------- --system
if [[ "$DO_SYSTEM" -eq 1 ]]; then
  step "system packages (apt, sudo)"
  command -v apt-get >/dev/null || die "--system supports apt-based systems only; install deploy/apt_packages.txt + python3 by hand (docs/INSTALL_MANUAL.md)"
  run_logged "apt-get update" sudo apt-get update
  # shellcheck disable=SC2046
  run_logged "apt-get install (deploy/apt_packages.txt + python3)" sudo apt-get install -y \
    $(list_file deploy/apt_packages.txt) python3 python3-venv python3-pip

  step "Isabelle $ISABELLE_VERSION -> /opt/isabelle"
  if [[ -x /opt/isabelle/bin/isabelle ]]; then
    ok "already installed: $(ISABELLE_COMPONENTS='' /opt/isabelle/bin/isabelle getenv -b ISABELLE_IDENTIFIER 2>/dev/null || echo /opt/isabelle)"
  else
    case "$(uname -m)" in
      aarch64|arm64) TARBALL="${ISABELLE_VERSION}_linux_arm.tar.gz" ;;
      *)             TARBALL="${ISABELLE_VERSION}_linux.tar.gz" ;;
    esac
    TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
    got=0
    for base in $(list_file deploy/isabelle_mirrors.txt | sed "s/VERSION/$ISABELLE_VERSION/g"); do
      printf '    %s ... ' "$base"
      if curl -fL --retry 2 --retry-connrefused --connect-timeout 20 --speed-limit 1000000 --speed-time 30 \
           -o "$TMP/$TARBALL" "$base/$TARBALL" >> "$LOG_FILE" 2>&1 && [[ -s "$TMP/$TARBALL" ]]; then
        printf 'OK\n'; got=1; break
      fi
      printf 'no\n'
    done
    [[ "$got" -eq 1 ]] || die "could not download $TARBALL from any mirror in deploy/isabelle_mirrors.txt"
    run_logged "extract" sudo tar -xf "$TMP/$TARBALL" -C /opt
    run_logged "install as /opt/isabelle" sudo mv "/opt/$ISABELLE_VERSION" /opt/isabelle
    run_logged "chown to $USER" sudo chown -R "$USER" /opt/isabelle
  fi
  if ! grep -qs 'ISABELLE_HOME=/opt/isabelle' ~/.profile; then
    printf '\nexport ISABELLE_HOME=/opt/isabelle\nexport PATH="$ISABELLE_HOME/bin:$PATH"\n' >> ~/.profile
    ok "added ISABELLE_HOME + PATH to ~/.profile (takes effect in new shells)"
  fi
  export ISABELLE_HOME=/opt/isabelle
fi

# ---------------------------------------------------------------- prerequisites
step "checking prerequisites"
if [[ -z "${ISABELLE_HOME:-}" ]]; then
  command -v isabelle >/dev/null || die "isabelle not on PATH and ISABELLE_HOME unset — run with --system, or install Isabelle (docs/INSTALL_MANUAL.md)"
  ISABELLE_HOME="$(isabelle getenv -b ISABELLE_HOME)"; export ISABELLE_HOME
fi
[[ -x "$ISABELLE_HOME/bin/isabelle" ]] || die "no isabelle launcher under ISABELLE_HOME=$ISABELLE_HOME"
export PATH="$ISABELLE_HOME/bin:$PATH"
ok "isabelle: $ISABELLE_HOME ($(ISABELLE_COMPONENTS='' isabelle getenv -b ISABELLE_IDENTIFIER))"
if [[ -n "${JAVA_HOME:-}" ]]; then
  note "WARNING: JAVA_HOME is exported ($JAVA_HOME). Isabelle resolves its own JDK and fails with"
  note "         'Unknown JAVA_HOME' under a foreign one — unset it in the server's environment."
fi
command -v fc-match >/dev/null || note "WARNING: fontconfig missing — Isabelle's JVM needs it (see deploy/apt_packages.txt)"
PYTHON="$(command -v python3 || command -v python || true)"
[[ -n "$PYTHON" ]] || die "python3 not found"
ok "python: $("$PYTHON" --version 2>&1)"

# ---------------------------------------------------------------- python env
if [[ "$DO_VENV" -eq 1 ]]; then
  step "python environment (.venv)"
  [[ -d .venv ]] || run_logged "create .venv" "$PYTHON" -m venv .venv
  PYTHON="$REPO/.venv/bin/python"
  run_logged "upgrade pip" "$PYTHON" -m pip install --upgrade pip
  run_logged "pip install server[mcp] + client" "$PYTHON" -m pip install -e ".[mcp]" -e ./client
fi

# ---------------------------------------------------------------- isabelle side
step "Isabelle component + backend"
run_logged "register REPL component (py4j/spliff contribs)" ./server/repl/Admin/init
run_logged "compile the Scala backend (isabelle scala_build)" isabelle scala_build
run_logged "ML heap cap + JVM GC logging (user settings)" ./server/repl/Admin/ensure_settings.sh

# ---------------------------------------------------------------- .env
step "configuring .env"
if [[ ! -f .env ]]; then
  cp .env.example .env
  TOKEN="$("$PYTHON" -c 'import secrets; print(secrets.token_hex(16))')"
  sed -i "s/^ISABELLE_ADMIN_TOKEN=.*/ISABELLE_ADMIN_TOKEN=$TOKEN/" .env
  ok "created .env from .env.example (admin token generated)"
else
  ok ".env exists, keeping it"
fi
mkdir -p logs
PORT="$(grep -E '^ISABELLE_SERVER_PORT=' .env | tail -1 | cut -d= -f2)"; PORT="${PORT:-8000}"
BASE="http://localhost:${PORT}"

# ---------------------------------------------------------------- --systemd
if [[ "$DO_SYSTEMD" -eq 1 ]]; then
  step "systemd service isabelle-pool-server (sudo)"
  UNIT="$(mktemp)"
  sed -e "s|@USER@|$USER|g" -e "s|@REPO@|$REPO|g" -e "s|@ISABELLE_HOME@|$ISABELLE_HOME|g" \
      -e "s|@PYTHON_DIR@|$(dirname "$PYTHON")|g" -e "s|@PYTHON@|$PYTHON|g" \
      deploy/isabelle-pool-server.service > "$UNIT"
  run_logged "install unit from deploy/isabelle-pool-server.service" sudo install -m 0644 "$UNIT" /etc/systemd/system/isabelle-pool-server.service
  rm -f "$UNIT"
  run_logged "systemctl daemon-reload" sudo systemctl daemon-reload
  run_logged "systemctl enable" sudo systemctl enable isabelle-pool-server
  run_logged "systemctl restart" sudo systemctl restart isabelle-pool-server
fi

# ---------------------------------------------------------------- --verify
if [[ "$DO_VERIFY" -eq 1 ]]; then
  step "smoke test"
  SRV=""
  if [[ "$DO_SYSTEMD" -eq 0 ]]; then
    note "starting the server in the background (log: logs/native_setup_verify.log)"
    ( set -a; . ./.env; set +a; exec "$PYTHON" -m server.app.main ) > logs/native_setup_verify.log 2>&1 &
    SRV=$!
    trap 'kill "$SRV" 2>/dev/null || true' EXIT
  fi
  wait_healthz "$BASE" 450 || die "server did not become healthy — see logs/native_setup_verify.log / journalctl -u isabelle-pool-server"
  smoke_test "$BASE" || die "smoke test failed"
  if [[ -n "$SRV" ]]; then
    kill "$SRV" 2>/dev/null || true; wait "$SRV" 2>/dev/null || true; trap - EXIT
    note "server stopped again"
  fi
fi

# ---------------------------------------------------------------- summary
echo
if [[ "$DO_SYSTEMD" -eq 1 ]]; then
  cat <<EOT
Isabelle Pool Server is installed as a service.
  API:           $BASE/         (health: /healthz, docs: /docs)
  Admin console: $BASE/admin    (token: ISABELLE_ADMIN_TOKEN in .env)
  Service:       systemctl status isabelle-pool-server   |   journalctl -u isabelle-pool-server -f
  Setup log:     $LOG_FILE
EOT
else
  cat <<EOT
Native setup done. Run the server in the foreground:
  set -a; source .env; set +a
  $PYTHON -m server.app.main        # then: curl $BASE/healthz   (1-2 min for the gateway JVM)
As a service: ./deploy/native_setup.sh --systemd        Prebuild heaps: isabelle build -b HOL-Library
Setup log:    $LOG_FILE
EOT
fi
