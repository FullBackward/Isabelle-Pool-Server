#!/usr/bin/env bash
# Tables, plots and REPORT_SNIPPET.md from results/ (any time; it reads every finished cell).
#   scripts/20_analyze.sh                       # results/
#   scripts/20_analyze.sh --results results/smoke --census results/smoke/census/census.json
set -u
source "$(dirname "$0")/_py.sh"
exec "$PY" -u -m trainmetrics.analyze "$@"
