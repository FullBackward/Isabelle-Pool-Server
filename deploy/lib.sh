#!/usr/bin/env bash
#
# Shared helpers for deploy/setup.sh (Docker) and deploy/native_setup.sh (host).
# Sourced, not executed. Conventions the two scripts share:
#
#   step "text"                 ==> headline for a phase
#   note "text"                     indented detail line
#   ok   "text"                     OK  result line
#   die  "text"                     FAIL line + log pointer, exit 1
#   run_logged "label" cmd...       run cmd with ALL its output appended to $LOG_FILE;
#                                   the terminal only sees "label ... OK (12s)" or the
#                                   FAIL line followed by the last 15 log lines
#   wait_healthz URL [seconds]      poll /healthz until alive (prints one OK line)
#   smoke_test BASE_URL             acquire -> enter_theory -> verify "lemma True by simp"
#                                   -> release; prints ONE result line
#   py args...                      python3, else python, else $PY_FALLBACK (a command
#                                   prefix such as "docker compose exec -T svc python")
#
# The caller sets LOG_FILE (and optionally PY_FALLBACK) before sourcing.

LOG_FILE="${LOG_FILE:-logs/setup.log}"
PY_FALLBACK="${PY_FALLBACK:-}"

step() { printf '\n==> %s\n' "$*"; }
note() { printf '    %s\n' "$*"; }
ok()   { printf '    OK  %s\n' "$*"; }
die()  { printf '    FAIL %s\n' "$*" >&2; [[ -s "$LOG_FILE" ]] && printf '    log: %s\n' "$LOG_FILE" >&2; exit 1; }

log_init() {
  mkdir -p "$(dirname "$LOG_FILE")"
  : > "$LOG_FILE"
  printf '# %s  %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$0 $*" >> "$LOG_FILE"
}

run_logged() {
  local label="$1"; shift
  printf '    %s ... ' "$label"
  printf '\n### %s\n$ %s\n' "$label" "$*" >> "$LOG_FILE"
  local t0=$SECONDS
  if "$@" >> "$LOG_FILE" 2>&1; then
    printf 'OK (%ds)\n' $((SECONDS - t0))
  else
    local rc=$?
    printf 'FAIL (exit %d)\n' "$rc"
    printf '    last lines of %s:\n' "$LOG_FILE" >&2
    tail -n 15 "$LOG_FILE" | sed 's/^/      | /' >&2
    exit "$rc"
  fi
}

py() {
  if command -v python3 >/dev/null 2>&1; then python3 "$@"
  elif command -v python >/dev/null 2>&1; then python "$@"
  elif [[ -n "$PY_FALLBACK" ]]; then $PY_FALLBACK "$@"
  else die "no python on this host (needed to parse JSON)"
  fi
}

json_field() {  # json_field KEY  < json
  py -c 'import json,sys; print(json.load(sys.stdin)[sys.argv[1]])' "$1"
}

wait_healthz() {  # wait_healthz BASE_URL [max_seconds]
  local base="$1" max="${2:-450}" t0=$SECONDS
  printf '    waiting for the gateway JVM (1-2 min, up to %ds) ... ' "$max"
  while (( SECONDS - t0 < max )); do
    if curl -fsS -m 3 "$base/healthz" >/dev/null 2>&1; then
      printf 'alive (%ds)\n' $((SECONDS - t0)); return 0
    fi
    sleep 5
  done
  printf 'TIMEOUT\n'; return 1
}

smoke_test() {  # smoke_test BASE_URL
  local base="$1" resp sid lease result secs
  printf '    acquire session, enter theory, verify "lemma True by simp" ... '
  resp="$(curl -fsS -m 600 -X POST "$base/api/v1/sessions/acquire" \
    -H 'Content-Type: application/json' -d '{"theories": ["Main"], "field": "HOL"}')" \
    || { printf 'FAIL (acquire)\n'; return 1; }
  sid="$(echo "$resp" | json_field session_id)"; lease="$(echo "$resp" | json_field lease_id)"
  curl -fsS -m 300 -X POST "$base/api/v1/sessions/$sid/enter_theory/Scratch" \
    -H 'Content-Type: application/json' -H "X-Lease-Id: $lease" -d '{"imports": ["Main"]}' >/dev/null \
    || { printf 'FAIL (enter_theory)\n'; return 1; }
  result="$(curl -fsS -m 120 -X POST "$base/api/v1/sessions/$sid/verify_chunk" \
    -H 'Content-Type: application/json' -H "X-Lease-Id: $lease" \
    -d '{"chunk": "lemma True by simp", "timeout": 60}')" \
    || { printf 'FAIL (verify_chunk)\n'; return 1; }
  printf '%s\n' "$result" >> "$LOG_FILE"
  curl -fsS -m 30 -X POST "$base/api/v1/sessions/$sid/release" -H "X-Lease-Id: $lease" >/dev/null 2>&1 || true
  if [[ "$(echo "$result" | json_field success)" == "True" ]]; then
    secs="$(echo "$result" | py -c 'import json,sys; print(round(json.load(sys.stdin)["execution_time"], 2))')"
    printf 'OK (%ss, session released)\n' "$secs"
  else
    printf 'FAIL: %s\n' "$(echo "$result" | json_field error)"
    return 1
  fi
}
