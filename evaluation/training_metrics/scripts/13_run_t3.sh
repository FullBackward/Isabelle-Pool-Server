#!/usr/bin/env bash
# T3 branch cost (results/t3/rep0): 200 proof states, a third per position stratum, each on a session created for
# it: checkpoint, verify the next block, restore, verify again, rollback; then a new node replaying the prefix on
# the same session (e) and a new session doing the same (f). Needs results/census/census.json. W = 1; a fresh
# server unless --ips-external. The cell prints the census replay time it repeats; about 1 h on the server.
#   nohup scripts/13_run_t3.sh > results/t3.log 2>&1 &
# Local container (HOL smoke corpus):
#   PYTHON=python scripts/13_run_t3.sh --local-smoke --ips-external --census results/smoke/census_3d/census.json --out results/smoke
set -u
source "$(dirname "$0")/_py.sh"
exec "$PY" -u -m trainmetrics.run_t3 "$@"
