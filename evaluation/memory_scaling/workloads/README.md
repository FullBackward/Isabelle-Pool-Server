# Workload theories

One fixed theory per heap, loaded into every session (P3) and re-checked concurrently under a new
theory name with a trailing marker lemma (P5). Recorded by sha256 in every cell manifest.

| Heap | File | Source | Lines |
|---|---|---|---|
| HOL | `CTL.thy` | Isabelle2026-RC2 `src/HOL/ex/CTL.thy` (imports Main) | 311 |
| HOL-Library | `Termination.thy` | Isabelle2026-RC2 `src/HOL/ex/Termination.thy` (imports Main, HOL-Library.Multiset) | 249 |
| HOL-Analysis | `Sum_Topology.thy` | this repo's `evaluation/HOL_corpus/processed/Sum_Topology.thy` (imports HOL-Analysis.Abstract_Topology) | 162 |

The two Isabelle files are copied verbatim from the distribution (BSD licence, see Isabelle's
`COPYRIGHT`); they are examples that check in well under a minute on a warm heap. Choose a
different file per heap in `config.yaml` if the server's Isabelle differs — the manifest records
whichever file ran.
