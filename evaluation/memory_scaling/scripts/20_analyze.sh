#!/usr/bin/env bash
# Tables, plots and the report snippet from results/ (rerun any time; reads every cell with a manifest).
set -u
HERE="$(cd "$(dirname "$0")/.." && pwd)"
cd "$HERE"
exec python -m memscale.analyze --results "${RESULTS:-$HERE/results}" "$@"
