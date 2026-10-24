#!/usr/bin/env bash
# Smoke (~15 min): census of three files, T1 at W = 1 and 2 (one repeat, 2 files per worker), the PIDE MCP row at
# W = 1 (its session starts on the heap: a PIDE problem shows here, not after T1's hour), the T6a calibration
# and T6b calibration cells (W = 1, one file each: the loop's cost; after the full census,
# trainmetrics.calibrate_t6 turns them into the T6 cell timeouts, design S8), analysis.
#   scripts/02_smoke.sh            # the server: the three HOL-Analysis smoke_files, fresh servers
#   scripts/02_smoke.sh --local    # inside the container: the local_smoke HOL files, the container's server
# Output under results/smoke/ (census/, t1/, pide/, t6a/, t6b/, tables/, REPORT_SNIPPET.md).
set -u
source "$(dirname "$0")/_py.sh"
OUT=results/smoke
rm -rf "$OUT"   # start clean: the runner skips ok cells, so stale cells would pass a smoke of new code (review c9c9197 S1)
if [ "${1:-}" = "--local" ]; then
  MODE=(--local-smoke --ips-external)
else
  FILES="$("$PY" -c 'import yaml; print(",".join(yaml.safe_load(open("config.yaml"))["smoke_files"]))')"
  MODE=(--files "$FILES")
fi
echo "== census $(date -u +%H:%M:%SZ)"
"$PY" -u -m trainmetrics.census "${MODE[@]}" --out "$OUT/census" || exit 1
echo "== T1 W=1,2 $(date -u +%H:%M:%SZ)"
MATRIX=(--workers 1,2 --reps 1 --quota 2 --census "$OUT/census/census.json" --out "$OUT" --rerun-partial)
[ "${1:-}" = "--local" ] && MATRIX+=(--local-smoke --ips-external)
"$PY" -u -m trainmetrics.run_matrix "${MATRIX[@]}"
rc=$?
echo "== T1 PIDE MCP row W=1 $(date -u +%H:%M:%SZ)"
PIDE=(--experiment pide --workers 1 --reps 1 --quota 2 --census "$OUT/census/census.json" --out "$OUT" --rerun-partial)
[ "${1:-}" = "--local" ] && PIDE+=(--local-smoke)
"$PY" -u -m trainmetrics.run_matrix "${PIDE[@]}" || rc=1
for style in t6a t6b; do
  echo "== $style calibration W=1, one file $(date -u +%H:%M:%SZ)"
  CAL=(--style "$style" --workers 1 --rep 0 --files-per-worker 1 --census "$OUT/census/census.json" --out "$OUT")
  if [ "${1:-}" = "--local" ]; then CAL+=(--local-smoke --ips-external); else CAL+=(--files "$FILES"); fi
  "$PY" -u -m trainmetrics.run_t6 "${CAL[@]}" || [ "$style" = t6b ] || rc=1   # t6b: the file may have no path
done
"$PY" -u -m trainmetrics.calibrate_t6 --cell "$OUT/t6a/w1/rep0" --cell-t6b "$OUT/t6b/w1/rep0" || rc=1
echo "== analysis $(date -u +%H:%M:%SZ)"
"$PY" -u -m trainmetrics.analyze --results "$OUT" --census "$OUT/census/census.json" || exit 1
exit $rc
