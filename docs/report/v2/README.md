# Technical report v2 — source

`main.tex` is the v2 source: the v1 draft revised with the memory-scaling results (2026-10-21) and the
training-metrics results (2026-10-27), after a style pass on the Kimina and AXLE technical reports (first
person for what we did, nominal headings, a contributions list, declarative prose; the two-reader framing kept).
Upload to the Overleaf project next to the existing `references.bib` and `figures/university_logo.png`,
which are not in this repository; `figures/*.png` here are the plots the source includes
(`host_delta_settled_mb.png`, `pss_peak_mb.png` from the memory-scaling runs; `training_throughput.png`,
`training_branch_cost.png` from the training-metrics runs).

No red `\todo{}` markers are left. The cache hit rate is stated as not
measured. The worked example uses the Isabelle-Humanize runs; Appendix A uses the real client API. No new
bibliography keys were introduced.

Numbers: the memory-scaling ones come from `docs/experiments/2026-10-20-memory-scaling-ips-vs-pide-mcp.md`
and `docs/experiments/2026-10-21-memory-scaling-series.csv`; the training-metrics ones from
`REPORT_SNIPPET.md` and `tables/` of the `training-metrics/` tree in
https://github.com/FullBackward/Isabelle-Pool-Server-results (the analysis of `evaluation/training_metrics/`
at commit e144f97 or later, run on the server's results tree). The body keeps the numbers a reader acts on
and one figure or table per question; the full per-cell tables are in the results repository.
