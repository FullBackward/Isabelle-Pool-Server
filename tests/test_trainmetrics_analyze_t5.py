"""Training metrics stage 3c-2: the T5 analysis — prover levels per file, the
share of blocks straddling a state change, the contention cell against T1's
cell on matched blocks, and the T5 part of REPORT_SNIPPET.md."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(
    0, str(Path(__file__).resolve().parents[1] / "evaluation" / "training_metrics")
)

from trainmetrics import analyze, analyze_t5  # noqa: E402


def _sample(t, provers):
    return {
        "t": t,
        "procs": [
            {"pid": pid, "role": "poly", "rss": int(mb * 2**20), "cpu_ticks": ticks}
            for pid, (mb, ticks) in provers.items()
        ],
    }


def _attempt(seed_id, family, budget, t, wall, settled):
    return {
        "seed_id": seed_id,
        "family": family,
        "goal_source": "synthetic",
        "budget_s": budget,
        "outcome": "timed_out",
        "wall_s": wall,
        "undo_s": wall - budget,
        "stuck_line": 2,
        "source_unchanged": True,
        "t_start": t,
        "t_settled": settled,
    }


def _blk(i, t, wall, proved=True):
    return {
        "index": i,
        "t_start": t,
        "wall_s": wall,
        "execution_time": wall - 0.01,
        "proved": proved,
    }


def _tree(tmp_path):
    root = tmp_path / "results"
    census = {
        "census_id": "cid",
        "totals": {"files": 2, "blocks": 4, "commands": 8},
        "files": [
            {
                "stem": s,
                "blocks": [
                    {
                        "index": i,
                        "proved": True,
                        "n_commands": 2,
                        "wall_s": 1.0,
                        "sha": "x",
                    }
                    for i in range(2)
                ],
            }
            for s in ("A", "B")
        ],
    }
    (root / "census").mkdir(parents=True)
    (root / "census" / "census.json").write_text(json.dumps(census))
    # the W = 1 cell: one file, one subst seed at 5 s, prover 10
    w1 = root / "t5" / "w1" / "rep0"
    w1.mkdir(parents=True)
    rec = {
        "stem": "A",
        "worker": 0,
        "t_start": 99.0,
        "acquire_s": 0.5,
        "enter_s": 0.1,
        "t_close": 107.0,  # after the close: a teardown row at 108, gone at 109
        "t_end": 110.0,
        "positions": [
            {
                "position": 1,
                "census_proved": True,
                "census_wall_s": 1.0,
                "verdict_match": True,
                "recovery_s": 1.2,
                "attempts": [_attempt("subst-0", "subst", 5.0, 100.0, 5.1, 106.6)],
            }
        ],
        "blocks": [],
    }
    (w1 / "positions.jsonl").write_text(json.dumps(rec) + "\n")
    rows = [
        _sample(99.0 + i, {10: (800 + 100 * min(i, 7), 100 * i)}) for i in range(9)
    ] + [_sample(108.0, {10: (300, 900)}), _sample(109.0, {})]
    (w1 / "samples.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    (w1 / "manifest.json").write_text(
        json.dumps({"experiment": "t5", "workers": 1, "rep": 0, "outcome": "ok"})
    )
    # the contention cell: worker 0 loops 10-20 on A, worker 1 replays B meanwhile
    w2 = root / "t5" / "w2" / "rep0"
    w2.mkdir(parents=True)
    a = {
        "stem": "A",
        "worker": 0,
        "t_start": 0.0,
        "acquire_s": 1.0,
        "enter_s": 0.5,
        "t_end": 31.0,
        "positions": [
            {
                "position": 1,
                "census_proved": True,
                "verdict_match": True,
                "attempts": [_attempt("w0:subst-0", "subst", 10.0, 10.0, 10.0, 21.5)],
            }
        ],
        "blocks": [_blk(0, 2.0, 1.0), _blk(1, 22.0, 1.0)],
    }
    b = {
        "stem": "B",
        "worker": 1,
        "t_start": 0.0,
        "acquire_s": 1.0,
        "enter_s": 0.5,
        "t_end": 40.0,
        "positions": [],
        "blocks": [_blk(0, 5.0, 1.0), _blk(1, 18.0, 3.0)],
    }  # 18-21: in worker 0's loop, then past its end
    (w2 / "positions.jsonl").write_text(json.dumps(a) + "\n" + json.dumps(b) + "\n")
    (w2 / "tiers.jsonl").write_text(
        json.dumps({"t": 1.0, "state": "ok", "used_mb": 900}) + "\n"
    )
    (w2 / "manifest.json").write_text(
        json.dumps(
            {
                "experiment": "t5_contention",
                "workers": 2,
                "rep": 0,
                "outcome": "ok",
                "server_executor_threads": 12,
            }
        )
    )
    # T1's W = 2 cell: the same blocks, each 1.0 s
    t1 = root / "t1" / "w2" / "rep0"
    t1.mkdir(parents=True)
    reps = [
        {"stem": s, "worker": w, "blocks": [_blk(i, 0.0, 1.0) for i in range(2)]}
        for w, s in ((0, "A"), (1, "B"))
    ]
    (t1 / "replays.jsonl").write_text("\n".join(json.dumps(r) for r in reps) + "\n")
    return root, census


def test_levels_straddles_and_matched_blocks(tmp_path):
    root, census = _tree(tmp_path)
    cells = analyze_t5.load_t5_cells(root)
    assert [int(c["manifest"]["workers"]) for c in cells] == [1, 2]
    lv = analyze_t5.file_levels(cells[0]["files"], cells[0]["samples"])
    assert lv == [
        {
            "stem": "A",
            "worker": 0,
            "start_mb": 900.0,
            "peak_mb": 1500.0,
            "end_mb": 1500.0,  # the row before t_close, not the teardown row (300 MB)
            "end_censored": False,
        }
    ]
    # review 0268dbe B1: without t_close (older records) the end is read at the
    # end of the file's last call (the seed attempt ending at 105.1), never in
    # the close; with no row before it the end is censored
    old_rec = {k: v for k, v in cells[0]["files"][0].items() if k != "t_close"}
    lv = analyze_t5.file_levels([old_rec], cells[0]["samples"])
    assert lv[0]["end_mb"] == 1400.0 and not lv[0]["end_censored"]
    lv = analyze_t5.file_levels([old_rec], cells[0]["samples"][-2:])
    assert lv[0]["end_mb"] is None and lv[0]["end_censored"]
    w2 = cells[1]["files"]
    assert analyze_t5.straddle_share(w2) == 0.25  # B's 18-21 spans the loop's end
    m = analyze_t5.matched_t1(w2, root, 2, 0)
    assert m["blocks"] == 4 and m["ratio_p50"] == 1.0 and m["sum_ratio"] == 1.5


def test_the_t5_snippet_and_tables(tmp_path):
    root, _census = _tree(tmp_path)
    assert analyze.main(["--results", str(root)]) == 0
    text = (root / "REPORT_SNIPPET.md").read_text()
    assert "## T5 bounded step" in text and "counts are maxima" in text
    assert "| subst | 5 | 1 | 1.000 | 1.000 |" in text
    assert "Contention at W = 2" in text and "on 4 matched blocks" in text
    assert "(W = 1, rep 0)" in text and "-x" not in text
    assert "not a leak measure" in text and "1.5 s quiet pause" in text
    assert (root / "tables" / "t5_bounded.csv").exists()
    assert (root / "tables" / "t5_contention.csv").exists()


def test_a_cell_without_a_replay_baseline_says_so():
    """Review 0268dbe S1: no "-x its time" sentence."""
    c = {
        "workers": 2,
        "rep": 0,
        "loops": 4,
        "loop_vs_replay_blocks": 0,
        "loop_vs_replay_slowdown": None,
        "loop_vs_replay_slowdown_p95": None,
        "straddle_share": 0.0,
        "t1_matched": None,
        "tiers": {},
        "executor_threads": 12,
        "by_state": [],
    }
    text = "\n".join(
        analyze_t5._contention_lines(c)
    )  # pylint: disable=protected-access
    assert "No replay baseline at this W" in text and "-x" not in text
