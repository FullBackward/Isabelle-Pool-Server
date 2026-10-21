# Technical report v2 — source

`main.tex` is the v2 source (2026-10-21), revised from the v1 draft with the memory-scaling results and a style pass after the Kimina and AXLE technical reports (first person for what we did, nominal headings, a contributions list, declarative prose; the two-reader framing kept).
Upload to the Overleaf project next to the existing `references.bib` and `figures/university_logo.png`,
which are not in this repository; `figures/*.png` here are the E1 plots the source includes.
Red `\todo{}` markers are the parts still unmeasured (training-reader metrics, the rollout figure, the PIDE MCP commit); cache hit rate is stated as not measured. The worked
example uses the Isabelle-Humanize runs; Appendix A uses the real client API. No new bibliography keys were introduced.
Numbers come from `docs/experiments/2026-10-20-memory-scaling-ips-vs-pide-mcp.md` and
`docs/experiments/2026-10-21-memory-scaling-series.csv`; the raw data is in
https://github.com/FullBackward/Isabelle-Pool-Server-results.
