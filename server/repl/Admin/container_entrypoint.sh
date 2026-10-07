#!/usr/bin/env bash
#
# DESCRIPTION: container entrypoint for the Isabelle Pool Server. Performs the
# setup that must survive volume/image drift, then execs the API server in the
# FOREGROUND (so `docker logs`/`docker compose logs` work and the container's
# lifetime equals the server's lifetime):
#
#   1. Component registration into the (possibly stale) isabelle_user_data
#      volume — docs/ISSUES.md Bug 7. Admin/init is a fast no-op when the
#      volume is already registered.
#   2. ML heap cap + JVM GC logging in the Isabelle user settings file
#      (Admin/ensure_settings.sh; idempotent, knob ISABELLE_ML_MAXHEAP_MB).
#   3. exec python -m server.app.main

set -euo pipefail

cd /app

./server/repl/Admin/init
./server/repl/Admin/ensure_settings.sh

echo "container_entrypoint: starting Isabelle Pool Server API server"
exec python -m server.app.main
