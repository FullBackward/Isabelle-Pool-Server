#!/usr/bin/env bash
# Smoke (~15 min): census of three files, T1 at W = 1 and 2 (one repeat, 2 files per worker), the T6a calibration
# cell (W = 1, one file: the loop's cost; after the full census, trainmetrics.calibrate_t6 turns it into
# t6.cell_timeout_s, design S8), analysis.
#   scripts/02_smoke.sh            # the server: the three HOL-Analysis smoke_files, fresh servers
#   scripts/02_smoke.sh --local    # inside the container: the local_smoke HOL files, the container's server
# Output under results/smoke/ (census/, t1/, t6a/, tables/, REPORT_SNIPPET.md).
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
echo "== T6a calibration W=1, one file $(date -u +%H:%M:%SZ)"
CAL=(--style t6a --workers 1 --rep 0 --files-per-worker 1 --census "$OUT/census/census.json" --out "$OUT")
if [ "${1:-}" = "--local" ]; then CAL+=(--local-smoke --ips-external); else CAL+=(--files "$FILES"); fi
"$PY" -u -m trainmetrics.run_t6 "${CAL[@]}" || rc=1
"$PY" -u -m trainmetrics.calibrate_t6 --cell "$OUT/t6a/w1/rep0" || rc=1
echo "== analysis $(date -u +%H:%M:%SZ)"
"$PY" -u -m trainmetrics.analyze --results "$OUT" --census "$OUT/census/census.json" || exit 1
exit $rc
