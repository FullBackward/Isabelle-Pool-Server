#!/usr/bin/env bash
# T1 throughput (and the T2 latency tables) over the config's W list, 3 repeats, resumable; then T1's PIDE MCP row
# (D3: PIDE MCP A at pide.workers W = 1, 8, edit-and-settle, trainmetrics.drive_pide_chunks).
# Needs results/census/census.json (scripts/01_census.sh). Run under nohup or tmux; one runner at a time.
#   nohup scripts/10_run_t1_t2.sh > results/t1.log 2>&1 &
#   scripts/10_run_t1_t2.sh --workers 1,2,4 --reps 1        # a part of the matrix
set -u
source "$(dirname "$0")/_py.sh"
"$PY" -u -m trainmetrics.run_matrix "$@"
rc=$?
# the PIDE MCP row (~20 min) at its own W (config pide.workers, D3: 1 and 8): a --workers meant for T1 is not passed
PIDE_ARGS=(); skip=0
for a in "$@"; do
  if [ "$skip" = 1 ]; then skip=0; continue; fi
  case "$a" in --workers) skip=1 ;; --workers=*) ;; *) PIDE_ARGS+=("$a") ;; esac
done
"$PY" -u -m trainmetrics.run_matrix --experiment pide ${PIDE_ARGS[@]+"${PIDE_ARGS[@]}"} || rc=1
exit $rc
