#!/usr/bin/env bash
# T5 bounded step at W = 1: one cell (results/t5/w1/rep0), the seeds of config t5 at 5 / 30 / 120 s.
# Needs results/census/census.json (scripts/01_census.sh). A fresh server unless --ips-external. ~1 h.
#   nohup scripts/11_run_t5.sh > results/t5.log 2>&1 &
# Local container (HOL smoke corpus):
#   PYTHON=python scripts/11_run_t5.sh --local-smoke --ips-external --census results/smoke/census/census.json --out results/smoke
set -u
source "$(dirname "$0")/_py.sh"
exec "$PY" -u -m trainmetrics.run_t5 "$@"
