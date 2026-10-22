#!/usr/bin/env bash
# The census (design B2): replay the corpus once at W = 1, max budget. ~20 min on the server.
#   scripts/01_census.sh                 # whole corpus -> results/census/census.json
#   scripts/01_census.sh --files A,B     # a subset (smoke); add --out results/smoke/census
set -u
source "$(dirname "$0")/_py.sh"
exec "$PY" -u -m trainmetrics.census "$@"
