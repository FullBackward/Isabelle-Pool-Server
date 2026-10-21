# Memory scaling: IPS vs PIDE MCP with many sessions

The experiment section for the technical report's comparison of IPS with PIDE MCP (Kappelmann's
`isabelle pide_mcp`) when one host serves many sessions. Design and plan:
`claude-work/2026-10-20-plan-memory-scaling-experiments/` (EXPERIMENT_DESIGN.md, IMPLEMENTATION_PLAN.md).
Everything is deterministic (no LLM): memory is a property of the connector and the workload.

What is compared, per heap (HOL, HOL-Library, HOL-Analysis) and session count k (1 … 32):

| tool | shape |
|---|---|
| `ips` | one IPS server (its own systemd scope), k leased sessions |
| `pide-a` | one `isabelle pide_mcp` process holding k sessions (the one-agent shape of its README) |
| `pide-b` | k `isabelle pide_mcp` processes with one session each (one process per agent) |

One cell = seven phases (P0 baseline, P1 start k sessions, P2 idle, P3 load the workload theory into
each, P4 settled, P5 concurrent re-check, P6 stop), one external `/proc` sampler for both tools
(RSS/PSS/USS per process, host used memory, the tool's cgroup), every per-session operation timed.
Outputs: `results/<tool>/<heap>/k<k>/rep<r>/{manifest.json, samples.jsonl}` (tool = `ips`, `pide-a`, `pide-b`).

## 0. Server prerequisites

- Ubuntu 22.04/24.04 with cgroup v2 and systemd (`systemd-run --user --scope` must work: over SSH run
  `loginctl enable-linger $USER` once), ≥ 128 GB RAM, ≥ 60 GB free disk, Python ≥ 3.12, git, no other
  Isabelle user on the box during the runs.
- One Isabelle install for both tools: **Isabelle2026-RC2** (the build PIDE MCP's `main` is pinned to;
  `isabelle version -i` is recorded in every manifest).

## 1. Install (once, ~30 min + heap builds)

```bash
# Isabelle + IPS (native), from a fresh clone of this repository on the server
git clone <this repo> ~/Isabelle-Pool-Server && cd ~/Isabelle-Pool-Server && git checkout <branch>
./deploy/native_setup.sh --system          # Isabelle2026-RC2 to /opt/isabelle, .venv, IPS component, ML heap cap
source .venv/bin/activate && pip install -e ".[mcp,eval]" -e ./client
export PATH=/opt/isabelle/bin:$PATH
# PIDE MCP as an Isabelle component (same install, same user)
git clone https://github.com/kappelmann/isabelle-pide-mcp ~/isabelle-pide-mcp   # branch main (RC2)
isabelle components -u ~/isabelle-pide-mcp
isabelle pide_mcp -?                        # builds the jar once, prints its usage (exits non-zero by design)
```

The ML heap cap IPS writes into `~/.isabelle/Isabelle2026-RC2/etc/settings` (`ML_OPTIONS … --maxheap`)
applies to every `poly` of both tools — that is intended (same cap for both) and recorded.

Every new shell needs the environment again before any script: `source .venv/bin/activate` in the
repo root and Isabelle's `bin` on `PATH` (`/opt/isabelle/bin`, or `$HOME/Isabelle2026-RC2/bin` for a
home-directory install without sudo).

## 2. Check the environment

```bash
cd evaluation/memory_scaling
scripts/00_check_env.sh          # prints the Appendix D block; every line must PASS
```

## 3. Build the heaps (once; HOL minutes, HOL-Library ~15 min, HOL-Analysis ~45 min)

```bash
scripts/01_build_heaps.sh        # records results/heap_build_times.txt
```

## 4. Smoke (≈ 10 min) — must pass before anything else

```bash
scripts/02_smoke.sh              # k = 1, 2 on HOL for all three tools; prints the E1 wall-time estimate
```

## 5. E1 — the capacity matrix (≈ 17 h from the smoke's estimate, plus a few hours for E2–E4; resumable)

Run it inside `tmux` or under `nohup`: the shell may disconnect, the runner must not.

```bash
nohup scripts/10_run_e1.sh > results/e1.log 2>&1 &     # or inside tmux
tail -f results/e1.log                                   # one line per cell
```
Rerun the same command after any interruption: finished cells are skipped. Options:
`scripts/10_run_e1.sh --tools ips,pide-b --heaps HOL --ks 1,2,4 --reps 1`.
**One runner per results tree:** the runner holds `results/RUNNER_LOCK` and refuses to start while
another runner is alive (two runners took the same cell and shared port 8000 on 2026-10-21; the
analysis flags such a cell as `contaminated` and leaves it out). A `partial` cell is skipped on
resume like a done one: `--rerun-partial` (or deleting its folder) runs it again.

## 6. E2, E3, E4

```bash
scripts/11_run_e2_latency.sh                 # IPS warm hit vs cold start, per heap
LIMIT=48G HEAP=HOL-Analysis scripts/12_run_e3_pressure.sh   # refusal (IPS) vs kernel kill (PIDE B) under a cgroup limit
scripts/13_run_e4_kill.sh                    # kill one prover among 8; do the others answer?
```

## 7. Analyse and hand over

```bash
scripts/20_analyze.sh            # results/tables/*.csv, results/plots/*.png, results/REPORT_SNIPPET.md
tar czf memory-scaling-results.tgz results/   # copy back; the snippet's tables go into the report section
```

## Fairness rules the scripts enforce

Same Isabelle build and user (same heaps, same ML heap cap), same workload files (sha256 in every
manifest), same phase durations and sampler; a fresh IPS server per cell in its own scope; PIDE MCP
sessions started with `no_build`; cold starts on both tools (IPS `reuse_dirty=False` on an empty pool);
the P5 marker is inserted after the first paragraph under the same theory name so both tools re-check
incrementally (a header change makes IPS reset the prover); IPS memory management ON. The analysis
reports two slopes (tool PSS and host used) and refuses to mix scoped and unscoped IPS cells.

## Layout

`memscale/` — `procs.py` (/proc readers), `sampler.py`, `workload.py`, `phases.py` (P0–P6 + manifest),
`drive_ips.py`, `drive_pide.py` + `mcp_stdio.py`, `run_cell.py` (one cell), `run_matrix.py` (E1),
`experiments.py` + `run_experiment.py` (E2–E4), `analyze.py`; `scripts/` as above; `workloads/`;
`config.yaml` (factors, phase durations, tool settings). Tests: `tests/test_memscale_*.py`.
