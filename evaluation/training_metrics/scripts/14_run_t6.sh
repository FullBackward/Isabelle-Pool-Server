#!/usr/bin/env bash
# T6 rollout rate: the T6a cells (whole-proof groups, D8/D9), then the T6b cells (step-level paths), each at the
# config's t6.workers (1, 8, 32) x t6.repeats (2), each cell in a child process with a fresh server, resumable.
# Needs results/census/census.json (made after stage 3d: T6b needs its command spans), and t6.t6a.cell_timeout_s /
# t6.t6b.cell_timeout_s set first (never below 7200) from
#   python -m trainmetrics.calibrate_t6 --cell results/smoke/t6a/w1/rep0 --cell-t6b results/smoke/t6b/w1/rep0 \
#     --census results/census/census.json
# (the smoke's calibration cells and the full census: each style's slowest worker x contention + the server start).
# About 3 h. Run under nohup or tmux; one runner per results tree.
#   nohup scripts/14_run_t6.sh > results/t6.log 2>&1 &
#   scripts/14_run_t6.sh --workers 1 --reps 1          # a part of the matrix (both styles)
# Local container (HOL smoke corpus, the container's server, the census 02_smoke.sh --local makes):
#   PYTHON=python scripts/14_run_t6.sh --local-smoke --ips-external --workers 1,2 --reps 1 \
#     --census results/smoke/census/census.json --out results/smoke
set -u
source "$(dirname "$0")/_py.sh"
rc=0
for style in t6a t6b; do
  "$PY" -u -m trainmetrics.run_matrix --experiment "$style" "$@" || rc=1
done
exit $rc
