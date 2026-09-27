#!/usr/bin/env bash
#
# DESCRIPTION: container entrypoint — ensure Isabelle components are registered
# in the (possibly stale) isabelle_user_data volume, then exec the container
# command. Fixes docs/ISSUES.md Bug 7: after an image rebuild the named volume
# shadows the image's /root/.isabelle, so component registration done at build
# time is lost ("Not found: py4j").
#
# Gated for speed: server/repl/Admin/init is idempotent (existing contribs are
# skipped), but we only run it when registration is actually missing — or
# when the volume still registers the PRE-MOVE location /app/repl (the
# component moved to /app/server/repl on 2026-09-27; init migrates it).

set -euo pipefail

cd /app

ISABELLE="${ISABELLE_HOME:-/opt/isabelle}/bin/isabelle"
ISABELLE_IDENTIFIER="$(ISABELLE_COMPONENTS='' "$ISABELLE" getenv -b ISABELLE_IDENTIFIER 2>/dev/null || echo Isabelle2025-2)"
USER_HOME="${HOME}/.isabelle/${ISABELLE_IDENTIFIER}"
COMPONENTS_FILE="${USER_HOME}/etc/components"

if [[ -f "$COMPONENTS_FILE" ]] \
    && grep -qx "/app/server/repl" "$COMPONENTS_FILE" \
    && ! grep -qx "/app/repl" "$COMPONENTS_FILE" \
    && grep -q "contrib/py4j" "$COMPONENTS_FILE" \
    && grep -q "contrib/spliff" "$COMPONENTS_FILE"; then
  echo "container_init: components already registered in volume, skipping init"
else
  echo "container_init: component registration missing or stale in volume — running server/repl/Admin/init"
  ./server/repl/Admin/init
fi

exec "$@"
