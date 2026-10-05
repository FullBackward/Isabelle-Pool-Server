# archive/ — read-only history

Nothing here is imported or run by the current server, client, MCP servers or
evaluation scripts (enforced by `tests/test_dependency_rules.py`). Kept for
provenance and for reproducing older results.

| Path | What it is | Status |
|---|---|---|
| `previous-works/IsabelleGym1.0/` | Tom Milan's IsabelleGym 1.0 sources | historical |
| `previous-works/*.pdf` | 2.0 report (`msc_20257720.pdf`), older API/client PDF docs | historical; live API spec is `/openapi.json` |
| `isabellegym2/local_gym/` | IsabelleGym 2.0 in-process gym (drives `server/repl/src/python/isabelle_client.py` directly, no server) | frozen 2026-09-27; DESIGN_CHOICES §1.1 records why the embedded model was abandoned |
| `isabellegym2/benchmark/` | AFP / session-pool benchmarks over the 2.0 gym | frozen |
| `isabellegym2/scripts/eval_smallstep_isabellegym.py` | local 2.0 baseline used for `smallstep_local_gym` runs | frozen; results in `evaluation/results/benchmark_runs.json` |
| `isabellegym2/scripts/eval_smallstep_qisabelle.py` | qIsabelle comparison (needs an external qIsabelle checkout) | frozen; results in `evaluation/results/benchmark_runs.json` |
| `install.sh` | pre-container local install | unmaintained (DESIGN_CHOICES §1.1) |

The archived scripts still contain their original `sys.path` / `local_gym`
imports; running them requires putting `archive/isabellegym2` on `PYTHONPATH`
and a matching `server/repl/src/python` API, which is not guaranteed to exist any more.
