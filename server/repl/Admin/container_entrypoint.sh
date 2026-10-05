#!/usr/bin/env bash
#
# DESCRIPTION: container entrypoint for the Isabelle Pool Server. Performs all
# one-time setup that must survive volume/image drift, then execs the API
# server in the FOREGROUND (so `docker logs`/`docker compose logs` work and
# the container's lifetime equals the server's lifetime):
#
#   1. Component registration into the (possibly stale) isabelle_user_data
#      volume — docs/ISSUES.md Bug 7 (delegated to container_init.sh).
#   2. ML heap cap: ensure the user settings file sets ML_OPTIONS with
#      --maxheap, so no single poly process (REPL session or `isabelle build`
#      child) can grow until the cgroup OOM-killer fires. This MUST live in
#      the user settings file: the polyml component's etc/settings forces
#      ML_OPTIONS="", clobbering any container-env/.env value, and a
#      non-empty ML_OPTIONS then wins over ML_OPTIONS32/64
#      (src/Pure/ML/ml_settings.scala). The override is a full replacement,
#      so the platform defaults are restated explicitly.
#
# The cap value is configurable via ISABELLE_ML_MAXHEAP_MB (default 9216).
# Idempotent: if any --maxheap is already present in the settings file, it is
# left untouched (manual operator tuning wins).

set -euo pipefail

cd /app

# 1. Component registration (no-op when already registered). container_init
#    execs its arguments, so `true` makes it a plain subroutine call here.
./server/repl/Admin/container_init.sh true

# 2. ML heap cap in the user settings file.
MAXHEAP_MB="${ISABELLE_ML_MAXHEAP_MB:-9216}"
ISABELLE="${ISABELLE_HOME:-/opt/isabelle}/bin/isabelle"
ISABELLE_HOME_USER="$("$ISABELLE" getenv -b ISABELLE_HOME_USER 2>/dev/null || echo "${HOME}/.isabelle")"
SETTINGS="${ISABELLE_HOME_USER}/etc/settings"

if [[ -f "$SETTINGS" ]] && grep -q -- "--maxheap" "$SETTINGS"; then
  echo "container_entrypoint: ML heap cap already present in $SETTINGS, leaving it"
else
  mkdir -p "$(dirname "$SETTINGS")"
  cat >> "$SETTINGS" <<EOF

# Isabelle Pool Server container_entrypoint: hard per-process ML heap cap. One
# pathological theory must fail gracefully (ML exception -> Build FAILED)
# instead of OOM-killing the container. Full replacement of the platform
# default, so --minheap/--enablegcsharing are restated explicitly.
ML_OPTIONS="--minheap 500 --enablegcsharing --maxheap ${MAXHEAP_MB}"
EOF
  echo "container_entrypoint: wrote ML heap cap (--maxheap ${MAXHEAP_MB}) to $SETTINGS"
fi

# 2b. JVM GC logging for Isabelle-launched JVMs (the gateway in particular).
# Same trap as ML_OPTIONS: env vars (ISABELLE_TOOL_JAVA_OPTIONS,
# JAVA_TOOL_OPTIONS) are clobbered/filtered by the isabelle toolchain's
# settings evaluation, so the only reliable injection point is the user
# settings file. %p keeps each JVM's log separate (gateway vs build tools).
if [[ -f "$SETTINGS" ]] && grep -q -- "-Xlog:gc" "$SETTINGS"; then
  echo "container_entrypoint: JVM GC logging already configured in $SETTINGS, leaving it"
else
  mkdir -p "$(dirname "$SETTINGS")" /app/logs
  cat >> "$SETTINGS" <<'EOF'

# Isabelle Pool Server container_entrypoint: GC logging for every Isabelle-launched
# JVM (gateway, build tools). The JVM's own output is the only source of
# truth for GC storms — the 2026-09-10 slowdown was undiagnosable without it.
ISABELLE_TOOL_JAVA_OPTIONS="$ISABELLE_TOOL_JAVA_OPTIONS -Xlog:gc*:file=/app/logs/isabelle-jvm-gc-%p.log:time,uptime,level,tags:filecount=3,filesize=10M"
EOF
  echo "container_entrypoint: wrote JVM GC logging (-Xlog:gc, /app/logs/isabelle-jvm-gc-%p.log) to $SETTINGS"
fi

# 3. Start the API server in the foreground.
echo "container_entrypoint: starting Isabelle Pool Server API server"
exec python -m server.app.main
