#!/usr/bin/env bash
# Build the corpus from the Isabelle under test (owner decision 2026-10-22): the theory names in
# corpus_stems.txt, taken from $ISABELLE_HOME/src/HOL/Analysis and processed by
# evaluation/scripts/process.py, into results/corpus/processed/ with results/corpus/MANIFEST.json.
# Seconds; no build, no server. Run it once per Isabelle installation, before 01_census.sh.
#   scripts/00a_prepare_corpus.sh                    # isabelle on PATH
#   scripts/00a_prepare_corpus.sh --src /path/to/src/HOL/Analysis
set -u
source "$(dirname "$0")/_py.sh"
exec "$PY" -u -m trainmetrics.prepare_corpus "$@"
