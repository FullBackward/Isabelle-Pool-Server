"""Training-reader metrics for the technical report (plan 2026-10-22): census,
throughput, verify latency, bounded step, agreement and branch cost of IPS on
the HOL-Analysis corpus. Deterministic, no LLM. Imports ``client`` and
``memscale`` only, never server code.

The one place the import path is set (plan review S8): the repository root (for
``client`` and ``evaluation.scripts.theory_splitter``) and
``evaluation/memory_scaling`` (for ``memscale``) are put on ``sys.path`` here.
"""

from __future__ import annotations

import sys
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent
EXPERIMENT_DIR = PACKAGE_DIR.parent  # evaluation/training_metrics
REPO_ROOT = EXPERIMENT_DIR.parents[1]
MEMSCALE_DIR = REPO_ROOT / "evaluation" / "memory_scaling"

for _p in (REPO_ROOT, MEMSCALE_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))
