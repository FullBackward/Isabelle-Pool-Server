#!/usr/bin/env bash
# T4 agreement (results/t4/rep0): part A cold-builds every corpus file as written against the census's in-session
# verdict; part B checks 200 sampled blocks x sorry / truncated / wrong fact in-session and cold (one mutation per
# build). Needs results/census/census.json. A fresh server started with ISABELLE_MAX_CONCURRENT_BIGSTEP_BUILDS=4
# (config ips.env) unless --ips-external. About 1 h at 4 builds.
#   nohup scripts/12_run_t4.sh > results/t4.log 2>&1 &
# Local container (HOL smoke corpus; its server's cap is 1):
#   PYTHON=python scripts/12_run_t4.sh --local-smoke --ips-external --census results/smoke/census/census.json --out results/smoke
set -u
source "$(dirname "$0")/_py.sh"
exec "$PY" -u -m trainmetrics.run_t4 "$@"
