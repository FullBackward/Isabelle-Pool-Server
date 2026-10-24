"""Training metrics stage 4c: the T4 analysis — part A's disagreements with
their cause, part B's table, safety findings, build times without cached
verdicts, the T4 part of REPORT_SNIPPET.md."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(
    0, str(Path(__file__).resolve().parents[1] / "evaluation" / "training_metrics")
)

from trainmetrics import analyze, analyze_t4  # noqa: E402

CTL_ERR = (
    '*** Bad bibtex_entry "McMillan-PhDThesis" (line 53 of "/tmp/x/CTL.thy")\n'
    "Unfinished session(s): Build_x\n0:00:04 elapsed time\n"
)


def _b(stem, i, variant, in_proved, cold, cached=False, outcome="failed"):
    return {
        "stem": stem,
        "index": i,
        "variant": variant,
        "applicable": True,
        "in_session": {"proved": in_proved, "outcome": outcome},
        "cold": {"accepted": cold, "wall_s": 0.01 if cached else 5.0, "cached": cached},
    }


def _tree(tmp_path, rep=0):
    root = tmp_path / "results"
    (root / "census").mkdir(parents=True, exist_ok=True)
    (root / "census" / "census.json").write_text(
        json.dumps({"census_id": "c", "files": [], "totals": {}})
    )
    d = root / "t4" / f"rep{rep}"
    d.mkdir(parents=True)
    part_a = [
        {
            "stem": "CTL",
            "in_session_proved": True,
            "first_unproved": None,
            "cold": {"accepted": False, "error": CTL_ERR, "wall_s": 6.0},
        },
        {
            "stem": "S",
            "in_session_proved": True,
            "first_unproved": None,
            "cold": {"accepted": True, "wall_s": 0.01, "cached": True},
        },
    ]
    part_b = [
        _b("S", 1, "unchanged", True, True, cached=True, outcome="ok"),
        _b("S", 1, "sorry", True, True, outcome="kept"),  # both unsound
        _b("S", 1, "truncated", False, False, outcome="kept"),
        _b("S", 1, "wrong_fact", True, True, outcome="ok"),  # ineffective
    ]
    (d / "part_a.jsonl").write_text("\n".join(json.dumps(r) for r in part_a) + "\n")
    (d / "part_b.jsonl").write_text("\n".join(json.dumps(r) for r in part_b) + "\n")
    (d / "manifest.json").write_text(
        json.dumps(
            {
                "experiment": "t4",
                "rep": rep,
                "outcome": "ok",
                "picks": [["S", 1]],
                "t4": {"build_concurrency": 4},
                "builds_bigstep_max_seen": 4,
            }
        )
    )
    return root


def test_the_t4_snippet_and_tables(tmp_path):
    root = _tree(tmp_path)
    assert analyze.main(["--results", str(root)]) == 0
    text = (root / "REPORT_SNIPPET.md").read_text()
    assert "## T4 agreement" in text and "1 of 2 agree" in text
    assert "## T1" not in text  # review 7e93e46 S4: no T1 cell, no empty T1 tables
    assert (
        '`*** Bad bibtex_entry "McMillan-PhDThesis"' in text
    )  # the telling line, not the elapsed time
    # review 05323b4 S1: the two kinds of safety finding, each named exactly
    assert "proved in-session and rejected cold: none" in text
    assert "both unsound): S#1" in text and "for inspection): S#1" in text
    assert (
        "1 served from the server's cache, not timed" in text
        and "at most 4 seen in flight" in text
    )
    assert (root / "tables" / "t4_agreement.csv").exists()
    assert (root / "tables" / "t4_mutations.csv").exists()


def test_every_t4_repeat_gets_its_paragraph(tmp_path):
    """Review 05323b4 S2: a second repeat is reported, not silently dropped."""
    _tree(tmp_path, rep=0)
    root = _tree(tmp_path, rep=1)
    assert analyze.main(["--results", str(root)]) == 0
    text = (root / "REPORT_SNIPPET.md").read_text()
    assert "## T4 agreement, rep 0" in text and "## T4 agreement, rep 1" in text
    agreement = (root / "tables" / "t4_agreement.csv").read_text().splitlines()
    assert agreement[0].startswith("rep,") and len(agreement) == 1 + 2 * 2


def test_cause_and_build_times():
    assert analyze_t4.cause(CTL_ERR).startswith("*** Bad bibtex_entry")
    assert (
        analyze_t4.cause("plain failure\n") == "plain failure"
        and analyze_t4.cause(None) == ""
    )
    cell = {
        "part_a": [
            {"cold": {"accepted": True, "wall_s": 4.0}},
            {"cold": {"accepted": True, "wall_s": 0.01, "cached": True}},
        ],
        "part_b": [
            {"variant": "sorry", "cold": {"accepted": False, "wall_s": 6.0}},
            {"variant": "unchanged", "cold": {"accepted": True, "wall_s": 4.0}},
        ],
    }
    bt = analyze_t4.build_times(cell)
    assert (
        bt["builds"] == 3 and bt["cached"] == 1 and bt["wall_p50_s"] == 5.0
    )  # 4.0 and 6.0, cached left out
