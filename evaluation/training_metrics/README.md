# Training metrics: IPS as a training and evaluation environment

The experiments behind the technical report's training-reader questions (report §4.3, the question table),
on the HOL-Analysis corpus, deterministic, no model in the loop:

| Question | Experiment | Script |
|---|---|---|
| How many verified rollouts per hour? | **T6** rollout rate: T6a whole-proof groups, T6b step-level paths (W 1 / 8 / 32) | `14_run_t6.sh` |
| (the step rate and its scaling; the comparison) | **T1** throughput (W 1 … 32) with the **PIDE MCP row** (W 1, 8) | `10_run_t1_t2.sh` |
| How long does one verify call take? | **T2** latency: a by-product of the T1 cells (D11) | — |
| Can I branch and backtrack cheaply? | **T3** branch cost: checkpoint / restore against a new node and a new session | `13_run_t3.sh` |
| Does "proved" in-session mean proved cold? | **T4** agreement: the corpus and a mutation suite against cold builds | `12_run_t4.sh` |
| Can one bad step stall a worker? | **T5** bounded step: seeds that loop, under budgets; one worker in a loop beside the others | `11_run_t5.sh` |

The design is `claude-work/2026-10-22-plan-training-metrics/EXPERIMENT_DESIGN.md` (v0.4), the plan
`IMPLEMENTATION_PLAN.md` (v1.7) beside it. `SERVER_GUIDE.md` is the same procedure as numbered steps for the
Ubuntu server; if the two differ, this README is right.

## 0. Prerequisites (the server)

- The memory-scaling setup (`../memory_scaling/SERVER_GUIDE.md` §3): IPS native, Isabelle2026-RC2 at
  `/opt/isabelle` (or `~/Isabelle2026-RC2`), the HOL-Analysis heap, PIDE MCP as an Isabelle component, the venv
  with `pip install -e ".[mcp,eval]" -e ./client`.
- Nothing else running Isabelle on the host. 128 GB, 32 cores (W = 32 sits on the server's default backend executor,
  `min(32, cpu_count + 4)`; every cell records it).
- Every IPS cell starts its own server with `config.yaml`'s `ips.env` (`ISABELLE_POOL_SIZE=40`,
  `ISABELLE_PARALLEL_PROOFS=2`, the bigstep build cap 4 for T4, …) and records it; port 8000 must be free.
- Run every script from `evaluation/training_metrics/`; `scripts/_py.sh` picks the Python (an active venv, the repo's
  `.venv`, else `python3`, or `PYTHON=…`) and sets the import path (`trainmetrics/__init__.py` adds the repo root and
  `../memory_scaling`).

## 1. The order (about 9 h of server time)

| # | Command | Time | Gives |
|---|---|---|---|
| 1 | `scripts/00_check_env.sh` | 1 min | PASS lines + the environment block (never builds) |
| 2 | `scripts/00a_prepare_corpus.sh` | seconds | `results/corpus/processed/` (94 theories from this Isabelle) + `MANIFEST.json` |
| 3 | `scripts/01_census.sh` | ~20 min | `results/census/census.json`: every block's command count, spans, verdict, time at W = 1 |
| 4 | `scripts/02_smoke.sh` | ~15 min | must pass: a 3-file census, T1 at W 1–2, the PIDE MCP row at W 1, the T6a and T6b calibration cells, the analysis |
| 5 | `python -m trainmetrics.calibrate_t6 --cell results/smoke/t6a/w1/rep0 --cell-t6b results/smoke/t6b/w1/rep0 --census results/census/census.json` | seconds | the T6 cell timeouts: set `t6.t6a.cell_timeout_s` and `t6.t6b.cell_timeout_s` in `config.yaml` |
| 6 | `scripts/10_run_t1_t2.sh` | ~1 h + 20 min | T1 cells `results/t1/w*/rep*`, then the PIDE MCP row `results/pide/w*/rep*` |
| 7 | `scripts/11_run_t5.sh` | ~1 h | `results/t5/w1/rep0`, `results/t5/w8/rep0` |
| 8 | `scripts/12_run_t4.sh` | ~1 h | `results/t4/rep0` (94 + 600 cold builds, 4 at a time) |
| 9 | `scripts/13_run_t3.sh` | ~1 h | `results/t3/rep0` (200 states) |
| 10 | `scripts/14_run_t6.sh` | ~3 h | `results/t6a/w*/rep*`, then `results/t6b/w*/rep*` |
| 11 | `scripts/20_analyze.sh` | 1 min | `results/tables/*.csv`, `results/plots/*.png`, `results/REPORT_SNIPPET.md` |
| 12 | `GH_TOKEN=… scripts/30_release_results.sh` | minutes | `results/` packed and attached to a GitHub Release (`TAG=trainmetrics-<date>`) |

- The census (3) must run **after** stage 3d (it records command spans; T6b needs them). `01_census.sh` exits 3 when
  it proves less than `census_min_proved_fraction` (0.9: stop and inspect), and 4 when a proved block's spans do not
  re-assemble it (the census is valid for T1–T5; only T6b's step split needs the spans).
- `02_smoke.sh` deletes `results/smoke/` first, so a stale cell can never pass a smoke of new code.
- Step 5: the slowest worker of each T6 matrix × `--contention` (3; once T1 has run, T1's slowdown at W = 32 is the
  better value) + the server start, never below 7200 s. Re-run it after a finished T6a W = 1 cell for real timeout
  shares.
- Long runs: under `nohup … &` or tmux, e.g. `nohup scripts/10_run_t1_t2.sh > results/t1.log 2>&1 &`. One runner per
  results tree (`results/RUNNER_LOCK`).
- Analysis at any time: `20_analyze.sh` reads every finished cell and recomputes every number from the raw records.

## 2. Resume and failures

- **Matrix scripts** (`10`, `14`; `run_matrix`): each cell is a child process with a wall timeout; a cell whose
  manifest is `ok` is skipped on the next start; `--rerun-partial` runs `error` / `timeout` / `terminated` cells
  again. A timed-out cell gets SIGTERM, then SIGKILL, and its server's process group is killed, so the next cell
  never meets an orphan. `--workers 1,8 --reps 1` runs a part.
- **Single-cell scripts**: `11_run_t5.sh` skips a finished cell (`--rerun` to repeat); `12_run_t4.sh` and
  `13_run_t3.sh` write their cell again when started again.
- Every cell writes its manifest at the start (`outcome: running`) and at the end (`ok`, `empty`, `error`,
  `terminated`, `timeout`); a failed cell is kept and reported, never silently re-run.
- Open leases: a killed cell's manifest lists `open_sessions`; the next fresh server starts empty.

## 3. Rules the code applies

- **Census first.** Every table refers to the census (`census_id` in every manifest): block budgets, size classes,
  T3 distances, T4 / T5 / T6 problems. Budget per block `min(600, max(30, 2 s × census commands))`; a seeded or
  mutated block keeps its original block's budget.
- **A session per file (D6)**: IPS fixes a session's context at acquisition, so every file is acquired with its own
  imports; uncovered imports are refused (409, stage S). Theory names are new on each session.
- **Same work per W**: the T1 assignment is one seeded sequence of corpus permutations; a smaller run's files are a
  prefix of a larger run's; `seed + rep` per repeat.
- **Verified = proved** (`success ∧ ¬proof_open ∧ ¬used_sorry ∧ ¬timed_out`); every rate and p95 has a timeout
  column beside it.
- **Outcome classes**: `memory`, `prover_died`, `session_gone` are their own classes; a dead session stops its file.
- **Integrity**: T3, T6a and T6b check the node source at points where no restore's pending work can be absorbed (a
  source read waits for a stable snapshot); T5 hashes the source before a seed and after the attempt.
- **Resolution**: `verify_chunk` re-reads the node every 0.1 s (`session.output_delay`); every latency steps by it;
  the PIDE MCP row polls at the same 0.1 s.
- **Resources**: the memscale sampler (PSS, CPU per process, host) once per second around every cell; the memory
  tier polled during T4 / T5 / T6.

## 4. Decisions (owner)

| | Decision |
|---|---|
| D1 | the HOL-Analysis corpus (94 theories, prepared from the Isabelle under test); smoke on three of its files |
| D2 | T1: 8 files per worker, a seeded nested assignment |
| D3 | the PIDE MCP row: topology A, W ∈ {1, 8}, edit-and-settle, a throughput row |
| D4 | T4 cold builds 4 at a time (`ISABELLE_MAX_CONCURRENT_BIGSTEP_BUILDS=4`) |
| D5 | T5 seeds: `subst`-loops and `metis` on a false goal |
| D6 | a session per file, acquired with the file's imports |
| D7 | the server refuses uncovered imports (409) |
| D8–D10 | T6: both styles; one fixed candidate mix per group of 8 (T6a); W ∈ {1, 8, 32} × 2 repeats |
| D11 | T2 latency only as a by-product of T1 |
| D12 | the PIDE MCP sessions run `parallel_proofs=2` as IPS's (both interactive level 3: they check alike) |

## 5. The local container (development only)

The Mac's IPS container has no HOL-Analysis heap (owner, 2026-10-22: never build it locally). Every script takes
`--local-smoke` (three Main-only theories on the HOL heap, `config.yaml` `local_smoke`) and `--ips-external` (the
container's running server), e.g. inside the container:
`cd /app/evaluation/training_metrics && PYTHON=python scripts/02_smoke.sh --local`. Take `LIVE_LOCK` in the impl
folder around live runs on the shared container.

## 6. Outputs

```
results/
├── corpus/processed/, corpus/MANIFEST.json      # 00a
├── census/census.json, replays.jsonl            # 01
├── smoke/                                       # 02 (deleted and rewritten by every smoke)
├── t1/w<W>/rep<r>/  pide/w<W>/rep<r>/           # 10: manifest.json, replays.jsonl, samples.jsonl
├── t5/w1/rep0/  t5/w8/rep0/                     # 11
├── t4/rep0/                                     # 12: part_a.jsonl, part_b.jsonl, tiers.jsonl
├── t3/rep0/                                     # 13: states.jsonl
├── t6a/w<W>/rep<r>/  t6b/w<W>/rep<r>/           # 14: events.jsonl, files.jsonl, tiers.jsonl
├── runner-logs/, RUNNER_LOCK
├── tables/*.csv  plots/*.png                    # 20
└── REPORT_SNIPPET.md                            # 20: the report's paragraphs, in the order of its questions
```

Hand-over: the release asset (step 12), and the raw tree copied into `FullBackward/Isabelle-Pool-Server-results`
under `training-metrics-<date>/` (as the memory-scaling data). The tables and plots the report uses go to
`docs/report/v2/figures/`; the `fig:rollout` figure is drawn from T6a's call sequence.

## 7. Layout

`trainmetrics/` — `corpus.py`, `prepare_corpus.py`, `census.py`, `spans.py`, `replay.py` (verify, budgets,
sessions); `harness.py` (server / tool driver, sampler, manifest, CLI), `run_matrix.py`, `procctl.py`, `serve.py`;
`run_t1.py`, `metrics_t12.py`, `drive_pide_chunks.py`, `analyze_pide.py` (T1, T2, the PIDE row); `seeds.py`,
`t5_core.py`, `run_t5.py`, `run_t5w8.py`, `contention.py`, `metrics_t5.py`, `analyze_t5.py` (T5); `mutate.py`,
`run_t4.py`, `metrics_t4.py`, `analyze_t4.py` (T4); `run_t3.py`, `metrics_t3.py`, `analyze_t3.py` (T3); `t6_core.py`,
`t6b.py`, `run_t6.py`, `metrics_t6.py`, `calibrate_t6.py`, `analyze_t6.py` (T6); `analyze.py`, `csvout.py`,
`samples.py`. Tests: `tests/test_trainmetrics_*.py` (no Isabelle needed).
