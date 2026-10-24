#!/usr/bin/env bash
# T6 rollout rate: the T6a cells (whole-proof groups, D8/D9) at the config's t6.workers (1, 8, 32) x t6.repeats (2),
# each in a child process with a fresh server, resumable; stage 7 adds the T6b paths. Needs
# results/census/census.json, and t6.cell_timeout_s set from the smoke's calibration (scripts/02_smoke.sh).
# About 2 h. Run under nohup or tmux; one runner per results tree.
#   nohup scripts/14_run_t6.sh > results/t6.log 2>&1 &
#   scripts/14_run_t6.sh --workers 1 --reps 1          # a part of the matrix
# Local container (HOL smoke corpus, the container's server):
#   PYTHON=python scripts/14_run_t6.sh --local-smoke --ips-external --workers 1,2 --reps 1 \
#     --census results/smoke/census_3d/census.json --out results/smoke
set -u
source "$(dirname "$0")/_py.sh"
exec "$PY" -u -m trainmetrics.run_matrix --experiment t6a "$@"
