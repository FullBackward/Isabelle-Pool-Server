# Development notes — index

The experiment logs that used to live in this file (1,050 lines) are now one
file per experiment under `docs/experiments/`, in date order:

| Date | File | Contents |
|---|---|---|
| 2026-07-16 | [`experiments/2026-07-16-mcp-comparison-mathd276.md`](experiments/2026-07-16-mcp-comparison-mathd276.md) | Single-problem (`mathd_algebra_276`) MCP comparison: the four IsabelleGym prompt variants, the I/Q prompts, the post-harness-fix result tables, methodology finding (in-session vs fresh-build verification) |
| 2026-07-30 | [`experiments/2026-07-30-pipeline-alignment-prompts-results.md`](experiments/2026-07-30-pipeline-alignment-prompts-results.md) | Pipeline alignment across the three systems, the full prompt texts per system/variant, the 10-rep summary table with sledgehammer usage, fairness protocol notes |
| 2026-07-31 | [`experiments/2026-07-31-new-problems-and-arbiter.md`](experiments/2026-07-31-new-problems-and-arbiter.md) | Two new problems (`imo_2019_p1`, `numbertheory_x5neqy2p4`) × two systems, I/Q sledgehammer usage addendum, the open arbiter multi-parent ROOT problem |

The raw transcripts behind these tables are under `evaluation/MCP-comparison/runs/`
(gitignored); the runner and `analyze.py` live in `evaluation/MCP-comparison/`.
Performance benchmarks (small-step / big-step throughput) are a separate thing:
`evaluation/results/benchmark_runs.json` + `evaluation/runs_analysis.ipynb`.

Architecture rationale is in `DESIGN_CHOICES.md`; the bug log and dated work
log are in `ISSUES.md`.
