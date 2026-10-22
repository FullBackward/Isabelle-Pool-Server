#!/usr/bin/env bash
# T5 bounded step: the W = 1 cell (results/t5/w1/rep0; seeds at 5 / 30 / 120 s), then the contention cell
# (results/t5/w8/rep0; one session in a 120 s loop while the others replay). A cell that is already ok is
# skipped (--rerun runs it again). Needs results/census/census.json. A fresh server per cell unless --ips-external.
#   nohup scripts/11_run_t5.sh > results/t5.log 2>&1 &
# Local container (HOL smoke corpus; the contention cell at W = 2, 5 s):
#   PYTHON=python scripts/11_run_t5.sh --local-smoke --ips-external --census results/smoke/census/census.json --out results/smoke
set -u
source "$(dirname "$0")/_py.sh"
"$PY" -u -m trainmetrics.run_t5 "$@"; rc1=$?
"$PY" -u -m trainmetrics.run_t5 --contention "$@"; rc2=$?
[ "$rc1" -eq 0 ] && [ "$rc2" -eq 0 ]
