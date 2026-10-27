# Technical report v2 — source

`main.tex` is the v2 source: the v1 draft revised with the memory-scaling results (2026-10-21), the
training-metrics results (2026-10-27) and the review of 2026-10-27 (`claude-work/2026-10-27-review-report-v2/REVIEW.md`,
applied the same day: the thesis sentence leads the abstract and the conclusion, the connector families sit
right after the introduction and name Isabelle's own `isabelle server` and the LSP family, the scaling headline
is the census-normalised 8.3× with the verify-only 3,181 chunks/min beside it, the verdict's scope is stated and
`axiomatization` is denylisted, a guidance section collects the rules a user acts on, the worked example is an
appendix without its prices). Style after the Kimina and AXLE technical reports: first person for what we did,
nominal headings, a contributions list, declarative prose; the two-reader framing kept.

Upload to the Overleaf project next to the existing `references.bib` and `figures/university_logo.png`, which
are not in this repository; `figures/*.png` here are the plots the source includes (`host_delta_settled_mb.png`,
`pss_peak_mb.png` from the memory-scaling runs, regenerated with the report's legend names;
`training_throughput.png`, `training_branch_cost.png` from the training-metrics runs). In `references.bib`,
give the PIDE MCP entry its version (v2, 2026-09-12, Zenodo 21519364): the comparison quotes it.

No `\todo{}` markers are left. The cache hit rate is stated as not measured. Appendix A uses the real client
API; the worked example (Appendix F) uses the Isabelle-Humanize runs. No new bibliography keys were introduced.

Numbers: the memory-scaling ones come from `docs/experiments/2026-10-20-memory-scaling-ips-vs-pide-mcp.md`
and `docs/experiments/2026-10-21-memory-scaling-series.csv`; the training-metrics ones from
`REPORT_SNIPPET.md` and `tables/` of the `training-metrics/` tree in
https://github.com/FullBackward/Isabelle-Pool-Server-results (the analysis of `evaluation/training_metrics/`
at commit e144f97 or later, run on the server's results tree). The body keeps the numbers a reader acts on
and one figure or table per question; the full per-cell tables are in the results repository.
