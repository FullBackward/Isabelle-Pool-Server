#!/usr/bin/env bash
# Check the training-metrics prerequisites; every line must say PASS. Prints the environment block.
#   scripts/00_check_env.sh                     # the server: HOL-Analysis heap, isabelle on PATH
#   HEAP=HOL ISABELLE="docker exec isabelle-pool-server isabelle" PYTHON=... scripts/00_check_env.sh
#                                               # the Mac: the container's Isabelle, the local-smoke heap
#   EXTERNAL=1 ...                              # runs with --ips-external: an IPS must ANSWER at the URL;
#                                               # without it (fresh-server runs) the URL must be FREE, because
#                                               # each run starts its own server there (review 0df4258 B1)
# This script never runs a command that can build: the heap check looks for the heap FILE
# (a dry run `isabelle build -n` prints nothing either way, and `build -l` without -n builds).
set -u
source "$(dirname "$0")/_py.sh"
URL="${IPS_URL:-http://127.0.0.1:8000}"
HEAP="${HEAP:-HOL-Analysis}"
ISABELLE="${ISABELLE:-isabelle}"   # may be several words, e.g. a docker exec prefix
ok=0; bad=0
check() { if eval "$2" >/dev/null 2>&1; then echo "PASS $1"; ok=$((ok+1)); else echo "FAIL $1 -- $3"; bad=$((bad+1)); fi; }
heap_present() {  # the heap file in the user or the system heap directory
  $ISABELLE env bash -c "ls \"\$ISABELLE_HEAPS\"/*/$HEAP \"\$ISABELLE_HEAPS_SYSTEM\"/*/$HEAP 2>/dev/null" | grep -q .
}
check "python $PY" "\"$PY\" -c 'import httpx, yaml'" "pip install -e '.[mcp,eval]' -e ./client"
check "package imports" "\"$PY\" -c 'import trainmetrics.census, client.async_client, memscale.sampler'" "run from a full checkout"
check "corpus prepared" "ls \"$HERE/results/corpus/processed\"/*.thy" "run scripts/00a_prepare_corpus.sh (builds it from this Isabelle's src/HOL/Analysis)"
check "isabelle ($ISABELLE)" "$ISABELLE version" "export PATH=\$HOME/Isabelle2026-RC2/bin:\$PATH (or /opt/isabelle/bin)"
check "heap $HEAP built" "heap_present" "on the SERVER: isabelle build -b $HEAP (memory_scaling scripts/01_build_heaps.sh); on the Mac never build, use HEAP=HOL"
if [ "${EXTERNAL:-0}" = "1" ]; then
  check "IPS answers at $URL (--ips-external runs)" "curl -sf $URL/readyz" "start the server you mean to measure"
else
  check "port free at $URL (fresh-server runs)" "! curl -s -o /dev/null $URL/healthz" "stop the IPS that answers there, or set EXTERNAL=1 and pass --ips-external"
fi
echo "== $ok passed, $bad failed"
"$PY" -c 'import json; from memscale.phases import environment_block as e; from trainmetrics.serve import isabelle_cmd as c; print(json.dumps(e(None, c()), indent=1))'
[ "$bad" -eq 0 ]
