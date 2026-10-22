#!/usr/bin/env bash
# Smoke (~10 min): census of three files, T1 at W = 1 and 2 (one repeat, 2 files per worker), analysis.
#   scripts/02_smoke.sh            # the server: the three HOL-Analysis smoke_files, fresh servers
#   scripts/02_smoke.sh --local    # inside the container: the local_smoke HOL files, the container's server
# Output under results/smoke/ (census/, t1/, tables/, REPORT_SNIPPET.md).
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
echo "== analysis $(date -u +%H:%M:%SZ)"
"$PY" -u -m trainmetrics.analyze --results "$OUT" --census "$OUT/census/census.json" || exit 1
exit $rc
