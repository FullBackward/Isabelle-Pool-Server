#!/usr/bin/env bash
# T1 throughput (and the T2 latency tables) over the config's W list, 3 repeats, resumable.
# Needs results/census/census.json (scripts/01_census.sh). Run under nohup or tmux; one runner at a time.
#   nohup scripts/10_run_t1_t2.sh > results/t1.log 2>&1 &
#   scripts/10_run_t1_t2.sh --workers 1,2,4 --reps 1        # a part of the matrix
set -u
source "$(dirname "$0")/_py.sh"
exec "$PY" -u -m trainmetrics.run_matrix "$@"
