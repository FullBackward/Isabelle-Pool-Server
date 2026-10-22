#!/usr/bin/env bash
# Check the training-metrics prerequisites; every line must say PASS. Prints the environment block.
set -u
source "$(dirname "$0")/_py.sh"
URL="${IPS_URL:-http://127.0.0.1:8000}"
ok=0; bad=0
check() { if eval "$2" >/dev/null 2>&1; then echo "PASS $1"; ok=$((ok+1)); else echo "FAIL $1 -- $3"; bad=$((bad+1)); fi; }
check "python $PY" "\"$PY\" -c 'import httpx, yaml'" "pip install -e '.[mcp,eval]' -e ./client"
check "package imports" "\"$PY\" -c 'import trainmetrics.census, client.async_client, memscale.sampler'" "run from a full checkout"
check "corpus present" "ls \"$REPO/evaluation/HOL_corpus/processed\"/*.thy" "the corpus ships with the repository"
check "isabelle on PATH" "command -v isabelle" "export PATH=\$HOME/Isabelle2026-RC2/bin:\$PATH (or /opt/isabelle/bin)"
check "HOL-Analysis heap" "! isabelle build -n -b HOL-Analysis | grep -q Building" "scripts of memory_scaling: 01_build_heaps.sh"
check "IPS reachable at $URL" "curl -sf $URL/readyz" "start the server (memory_scaling README, 'IPS native')"
echo "== $ok passed, $bad failed"
"$PY" -c 'import json; from memscale.phases import environment_block as e; print(json.dumps(e(), indent=1))'
[ "$bad" -eq 0 ]
