"""Result schema, JSONL recording, timing and token aggregation.

Schema follows horizontal-comparison-framework/Framework.md exactly.
"""
from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class AttemptResult:
    # Dimensions
    system: str
    problem: str
    repeat: int
    model_id: str = ""
    model_provider: str = ""
    model_temperature: float = 0.0
    variant: str = (
        "baseline"  # prompt/tool variant (--prompt-variant); "baseline" = the fixed prompt
    )

    # Outcome
    arbiter_solved: bool = False         # ground truth — used in headline numbers
    agent_claimed_solved: bool = False   # MCP's own signal (for agreement analysis)

    # Effort
    rounds: int = 0
    n_tool_calls: int = 0
    n_truncated_rounds: int = 0          # rounds cut at max_tokens (finish_reason=length)
    n_nudge_rounds: int = 0              # no-tool-call rounds answered with a harness nudge
    # (productive rounds = rounds - n_nudge_rounds)

    # Latency
    wall_s: float = 0.0                  # PRIMARY: full attempt wall-clock
    setup_s: float | None = None         # attempt start → timer start (excluded from wall_s;
    # makes the warm/cold setup asymmetry visible)
    first_tool_s: float | None = None    # duration of the first MCP tool call (warmup proxy)
    prover_s: float | None = None        # SECONDARY: summed MCP tool/eval time
    round_latencies: list[float] = field(default_factory=list)
    model_s: float | None = None         # DERIVED/optional: ≈ wall_s - prover_s

    # Token usage
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    cached_tokens: int = 0               # cached prompt tokens (subset of input_tokens)

    # Bookkeeping
    final_thy_path: str | None = None
    error: str | None = None

    def finalize(self) -> None:
        """Recompute derived fields after all primary fields are populated."""
        self.total_tokens = self.input_tokens + self.output_tokens
        if self.prover_s is not None and self.wall_s > 0:
            self.model_s = round(self.wall_s - self.prover_s, 2)

    def to_json(self) -> str:
        self.finalize()
        return json.dumps(asdict(self), default=str)


class Timer:
    """Simple wall-clock timer for a full attempt."""

    def __init__(self):
        self.t0: float | None = None
        self.t1: float | None = None

    def start(self) -> None:
        self.t0 = time.time()

    def stop(self) -> float:
        self.t1 = time.time()
        return self.elapsed()

    def elapsed(self) -> float:
        if self.t0 is None:
            return 0.0
        return (self.t1 or time.time()) - self.t0


class TokenAggregator:
    def __init__(self):
        self.input_tokens = 0
        self.output_tokens = 0
        self.cached_tokens = 0

    def add(self, usage: dict[str, int]) -> None:
        self.input_tokens += usage.get("input_tokens", 0)
        self.output_tokens += usage.get("output_tokens", 0)
        self.cached_tokens += usage.get("cached_tokens", 0)


def append_result(path: Path, result: AttemptResult) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(result.to_json() + "\n")


def load_results(path: Path) -> list[AttemptResult]:
    rows: list[AttemptResult] = []
    if not path.exists():
        return rows
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rows.append(AttemptResult(**json.loads(line)))
    return rows
