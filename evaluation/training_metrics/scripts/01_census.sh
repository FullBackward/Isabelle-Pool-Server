#!/usr/bin/env bash
# The census (design B2): replay the corpus once at W = 1, max budget. ~20 min on the server.
# It starts a fresh IPS server (as the T1 cells do) unless --ips-external is given.
#   scripts/01_census.sh                              # whole corpus -> results/census/census.json
#   scripts/01_census.sh --files A,B --out results/smoke/census      # a subset
#   scripts/01_census.sh --local-smoke --ips-external --out results/smoke/census   # the container
set -u
source "$(dirname "$0")/_py.sh"
exec "$PY" -u -m trainmetrics.census "$@"
