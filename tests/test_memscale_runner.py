"""memory-scaling stage 4: the matrix runner's cell order, resume, budget
rule and memory-ceiling stop (no tools: run_one is stubbed)."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(
    0, str(Path(__file__).resolve().parents[1] / "evaluation" / "memory_scaling")
)

from memscale import run_matrix  # noqa: E402

CFG = {
    "heaps": {"HOL": {"ks": [1, 2, 16]}, "HOL-Analysis": {"ks": [1, 2]}},
    "repeats": 2,
    "large_k": 16,
    "repeats_large_k": 1,
    "stop_when_host_used_gb_exceeds": 110,
    "phases": {"baseline_s": 60, "baseline_short_s": 10},
}


def test_cells_order_and_budget_rule():
    plan = list(run_matrix.cells(CFG, ["ips", "pide-b"]))
    assert plan[:5] == [
        ("ips", "HOL", 1, 0),
        ("ips", "HOL", 1, 1),
        ("ips", "HOL", 2, 0),
        ("ips", "HOL", 2, 1),
        ("ips", "HOL", 16, 0),
    ]
    assert ("ips", "HOL", 16, 1) not in plan  # large k: one repeat
    assert plan[-1] == ("pide-b", "HOL-Analysis", 2, 1)
    assert list(run_matrix.cells(CFG, ["ips"], heaps=["HOL"], ks=[2], reps=1)) == [
        ("ips", "HOL", 2, 0)
    ]


def _write_manifest(
    out: Path, tool: str, heap: str, k: int, rep: int, outcome: str
) -> None:
    d = out / run_matrix.TOOL_DIR[tool] / heap / f"k{k}" / f"rep{rep}"
    d.mkdir(parents=True, exist_ok=True)
    (d / "manifest.json").write_text(json.dumps({"outcome": outcome}))


def test_resume_skips_done_cells_and_the_ceiling_stops_a_series(tmp_path, monkeypatch):
    out = tmp_path / "results"
    cfg_path = tmp_path / "config.yaml"
    import yaml

    cfg_path.write_text(yaml.safe_dump(CFG))
    _write_manifest(out, "ips", "HOL", 1, 0, "ok")  # already run -> skipped
    _write_manifest(out, "ips", "HOL", 1, 1, "running")  # interrupted -> rerun
    ran = []
    used = {"gb": 50.0}

    def fake_run_one(args, tool, heap, k, rep, baseline_s):
        ran.append((tool, heap, k, rep, baseline_s))
        _write_manifest(out, tool, heap, k, rep, "ok")
        if (tool, heap, k) == ("ips", "HOL", 2):
            used["gb"] = 120.0  # the host filled up after this cell
        return "ok"

    monkeypatch.setattr(run_matrix, "run_one", fake_run_one)
    monkeypatch.setattr(run_matrix, "host_used_gb", lambda: used["gb"])
    rc = run_matrix.main(
        [
            "--config",
            str(cfg_path),
            "--out",
            str(out),
            "--tools",
            "ips",
            "--heaps",
            "HOL",
        ]
    )
    assert rc == 0
    assert ran[0] == (
        "ips",
        "HOL",
        1,
        1,
        60.0,
    )  # first cell of the series: the full baseline
    assert ran[1] == ("ips", "HOL", 2, 0, 10.0)  # later cells: the short baseline
    assert ("ips", "HOL", 16, 0, 10.0) not in ran  # the series stopped at the ceiling
    assert ("ips", "HOL", 1, 0, 60.0) not in ran  # the done cell was skipped


def test_one_runner_per_results_tree_and_rerun_partial(tmp_path, monkeypatch):
    """A live RUNNER_LOCK refuses a second runner (two runners took the same
    cell on 2026-10-21); a stale lock is ignored; --rerun-partial re-runs a
    partial cell that resume would otherwise skip."""
    out = tmp_path / "results"
    cfg_path = tmp_path / "config.yaml"
    import yaml

    cfg_path.write_text(yaml.safe_dump(CFG))
    _write_manifest(out, "ips", "HOL", 1, 0, "partial")
    ran = []

    def fake_run_one(args, tool, heap, k, rep, baseline_s):
        ran.append((tool, heap, k, rep))
        _write_manifest(out, tool, heap, k, rep, "ok")
        return "ok"

    monkeypatch.setattr(run_matrix, "run_one", fake_run_one)
    monkeypatch.setattr(run_matrix, "host_used_gb", lambda: 10.0)
    base = [
        "--config",
        str(cfg_path),
        "--out",
        str(out),
        "--tools",
        "ips",
        "--heaps",
        "HOL",
        "--ks",
        "1",
    ]
    lock = out / "RUNNER_LOCK"
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text(f"{os.getpid()} now\n")  # this very process: alive
    assert run_matrix.main(base) == 2 and ran == []
    lock.write_text("999999999 stale\n")  # no such pid: stale, ignored
    assert run_matrix.main(base) == 0
    assert (
        ran == [("ips", "HOL", 1, 1)] and not lock.exists()
    )  # partial skipped, lock gone
    assert run_matrix.main(base + ["--rerun-partial"]) == 0
    assert ran[-1] == ("ips", "HOL", 1, 0)


def test_dry_run_runs_nothing(tmp_path, capsys):
    cfg_path = tmp_path / "config.yaml"
    import yaml

    cfg_path.write_text(yaml.safe_dump(CFG))
    rc = run_matrix.main(
        [
            "--config",
            str(cfg_path),
            "--out",
            str(tmp_path / "r"),
            "--tools",
            "pide-a",
            "--dry-run",
        ]
    )
    assert (
        rc == 0
        and "would run pide-a HOL k=1 rep=0 (baseline 60.0s)" in capsys.readouterr().out
    )
