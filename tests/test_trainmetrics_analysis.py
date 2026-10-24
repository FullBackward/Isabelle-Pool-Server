"""Training metrics, stage 2 (plan v1.3), part 3: the T1/T2 metrics and the
analysis on a synthetic results tree (census-relative slowdown, verify-only
rate, per-pid CPU, T2 classes without partial census rows)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(
    0, str(Path(__file__).resolve().parents[1] / "evaluation" / "training_metrics")
)

from trainmetrics import analyze, corpus, metrics_t12  # noqa: E402

THY = """theory {name}
  imports Main
begin

lemma {name}_1: "True" by simp

lemma {name}_2: "x = x" by simp

end
"""


def _row(t, procs, busy=None, total=None, usage=None, pss=100):
    return {
        "t": t,
        "clk_tck": 100,
        "procs": [{"pid": p, "role": r, "cpu_ticks": c} for p, r, c in procs],
        "host_cpu": {"busy": busy, "total": total},
        "cgroup_cpu": {"usage_usec": usage},
        "sum": {"total": {"pss": pss * 2**20}},
        "host": {"used": 1000 * 2**20},
    }


def _corpus(tmp_path, names=("A", "B", "C")):
    d = tmp_path / "thy"
    d.mkdir()
    for n in names:
        (d / f"{n}.thy").write_text(THY.format(name=n))
    return corpus.load_corpus(str(d))


def _census(files, bad=()):
    return {
        "census_id": "cid",
        "totals": {"files": len(files), "blocks": 2 * len(files), "commands": 4},
        "files": [
            {
                "stem": f.stem,
                "enter_s": 0.1,
                "enter_error": "Bad import" if f.stem in bad else None,
                "blocks": [
                    {
                        "index": i,
                        "sha": f.block_sha[i],
                        "n_commands": 2,
                        "count_partial": False,
                        "wall_s": 0.2,
                    }
                    for i in range(len(f.blocks))
                ],
            }
            for f in files
        ],
    }


def _cell_tree(root, w, rep, census, *, wall=0.1, timed_out=False):
    d = root / "t1" / f"w{w}" / f"rep{rep}"
    d.mkdir(parents=True)
    reps = []
    for worker in range(w):
        for f in census["files"]:
            reps.append(
                {
                    "stem": f["stem"],
                    "worker": worker,
                    "enter_s": 0.05,
                    "enter_error": None,
                    "blocks": [
                        {
                            "index": b["index"],
                            "wall_s": wall,
                            "proved": not timed_out,
                            "timed_out": timed_out,
                            "n_commands": 2,
                            "error": None,
                        }
                        for b in f["blocks"]
                    ],
                }
            )
    (d / "replays.jsonl").write_text("\n".join(json.dumps(r) for r in reps) + "\n")
    manifest = {
        "experiment": "t1",
        "workers": w,
        "rep": rep,
        "outcome": "ok",
        "census_id": census["census_id"],
        "window_start": 1000.0,
        "window_end": 1000.0 + 60.0,
        "acquire": [{"worker": i, "acquire_s": 3.0} for i in range(w)],
        "nodes_per_session": {str(i): len(census["files"]) for i in range(w)},
    }
    (d / "manifest.json").write_text(json.dumps(manifest))
    rows = [
        _row(
            1000 + t,
            [(1, "jvm", 100 * t), (2, "poly", 300 * t)],
            busy=50 * t,
            total=100 * t,
        )
        for t in range(0, 61, 10)
    ]
    (d / "samples.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n")


def test_metrics_and_analysis_on_a_synthetic_tree(tmp_path):
    files = _corpus(tmp_path)
    census = _census(files)
    census["files"][0]["blocks"][1]["count_partial"] = True  # left out of T2
    root = tmp_path / "results"
    _cell_tree(root, 1, 0, census, wall=0.1)
    _cell_tree(root, 2, 0, census, wall=0.1)
    _cell_tree(root, 2, 1, census, wall=0.3, timed_out=False)
    cells = metrics_t12.load_t1_cells(root)
    rows = [metrics_t12.summarize_cell(c, census) for c in cells]
    r1 = next(r for r in rows if r["workers"] == 1)
    # 6 verified chunks in 0.6 s of verify time -> 600/min; slowdown = (3*0.05+6*0.1)/(3*(0.1+0.4))
    assert r1["chunks_per_min"] == 600.0 and r1["commands_per_min"] == 1200.0
    assert r1["chunks_per_min_incl_setup"] == 6.0 and r1["slowdown_vs_census"] == 0.5
    assert r1["cpu_cores_mean"] == 4.0 and r1["host_cpu_util"] == 0.5
    assert r1["census_match"] and r1["timeout_rate"] == 0.0
    series = metrics_t12.series_t1(rows)
    s2 = next(s for s in series if s["workers"] == 2)
    assert s2["repeats"] == 2 and s2["chunks_per_min"] == pytest.approx(800.0)
    # speedup on the census work rate (review c9c9197 S5): W=1 does 1.5 census-s
    # in 60 s, W=2 3.0 census-s in 60 s -> 2.0, whatever chunks/min say
    assert r1["census_work_rate"] == 0.025
    assert s2["speedup"] == pytest.approx(2.0) and s2["efficiency"] == pytest.approx(
        1.0
    )
    assert s2["chunks_per_min"] == pytest.approx(800.0)  # raw rate kept as a column
    t2 = metrics_t12.t2_table(cells, census, [1, 2])
    by = {(r["workers"], r["class"]): r for r in t2}
    assert by[(1, "2-9")]["n"] == 5  # 6 blocks, one with a partial census count
    assert by[(1, "2-9")]["ms_per_command_p50"] == 50.0
    assert (
        analyze.main(["--results", str(root), "--census", _write(tmp_path, census)])
        == 0
    )
    assert (root / "tables" / "t1_series.csv").exists()
    text = (root / "REPORT_SNIPPET.md").read_text()
    assert "| 600.0 |" in text and "Per-call floor" not in text  # no 1-command class
    assert "census work rate" in text and "1 with no command" not in text


def _write(tmp_path, census):
    p = tmp_path / "census.json"
    p.write_text(json.dumps(census))
    return str(p)


def test_timed_out_calls_are_not_in_the_latency_and_zero_counts_have_no_class(
    tmp_path,
):
    """Review c9c9197 S2/S3."""
    files = _corpus(tmp_path)
    census = _census(files)
    census["files"][1]["blocks"][0]["n_commands"] = 0  # a comment-only block
    root = tmp_path / "results"
    _cell_tree(root, 1, 0, census, wall=0.1)
    _cell_tree(root, 4, 0, census, wall=9.0, timed_out=True)
    cells = metrics_t12.load_t1_cells(root)
    rows = {
        r["workers"]: r for r in (metrics_t12.summarize_cell(c, census) for c in cells)
    }
    assert rows[4]["timeout_rate"] == 1.0 and rows[4]["latency_p50_s"] is None
    assert rows[1]["latency_p50_s"] == 0.1
    assert metrics_t12.size_class(0) is None and metrics_t12.size_class(None) is None
    assert metrics_t12.size_class(1) == "1" and metrics_t12.size_class(12) == "10+"
    assert metrics_t12.zero_count_blocks(census) == 1
    t2 = metrics_t12.t2_table(cells, census, [1])
    assert sum(r["n"] for r in t2) == 5  # 6 blocks, the zero-command one has no class
    text = analyze.snippet_t12(metrics_t12.series_t1(list(rows.values())), t2, census)
    assert "1 with no command" in text


def test_external_server_cells_show_no_resource_columns(tmp_path):
    """Review 1d9d674 S1: an external server's tree holds other clients."""
    files = _corpus(tmp_path)
    census = _census(files)
    root = tmp_path / "results"
    _cell_tree(root, 1, 0, census)
    m = root / "t1" / "w1" / "rep0" / "manifest.json"
    d = json.loads(m.read_text())
    d["server"] = {"external": True}
    m.write_text(json.dumps(d))
    rows = [
        metrics_t12.summarize_cell(c, census) for c in metrics_t12.load_t1_cells(root)
    ]
    series = metrics_t12.series_t1(rows)
    assert rows[0]["external"] and series[0]["external"]
    text = analyze.snippet_t12(series, [], census)
    assert "external server" in text
    row = next(x for x in text.splitlines() if x.startswith("| 1 |"))
    assert row.rstrip().endswith("| - | - |")  # host CPU and PSS blanked


def test_census_relative_numbers_count_only_replays_that_ran(tmp_path):
    """Review 34414a6 B1: a refused acquisition did no work."""
    files = _corpus(tmp_path)
    census = _census(files)
    root = tmp_path / "results"
    _cell_tree(root, 1, 0, census, wall=0.1)
    rpath = root / "t1" / "w1" / "rep0" / "replays.jsonl"
    reps = [json.loads(x) for x in rpath.read_text().splitlines()]
    reps[2] = {
        **reps[2],
        "acquire_error": "503 memory_pressure",
        "blocks": [],
        "enter_s": 0,
    }
    for r in reps[:2]:
        r["acquire_s"] = 0.25
    rpath.write_text("\n".join(json.dumps(r) for r in reps) + "\n")
    (row,) = [
        metrics_t12.summarize_cell(c, census) for c in metrics_t12.load_t1_cells(root)
    ]
    # 2 files ran: census time 2 x 0.5 s over 60 s; the refused file is skipped
    assert row["census_work_rate"] == pytest.approx(1.0 / 60, abs=1e-4)
    assert row["skipped_files"] == 1 and row["skipped_census_s"] == 0.5
    # replay time = 2 x (0.25 acquire + 0.05 enter + 2 x 0.1 verify) = 1.0 s
    assert row["acquire_share"] == 0.5 and row["slowdown_vs_census"] == 1.0


def test_the_snippet_follows_the_report_questions(tmp_path, monkeypatch):
    """Review 7e93e46 S4: T6 (rollouts per hour) before T1 (+ the PIDE row and
    T2), then T3, T4, T5; the count line names the PIDE cells too."""
    from trainmetrics import analyze  # pylint: disable=import-outside-toplevel

    monkeypatch.setattr(analyze, "write_t12", lambda *a: ("## T1 x", 2, 1, 3))
    monkeypatch.setattr(analyze, "write_t5", lambda *a: ("## T5 x", 1))
    monkeypatch.setattr(analyze, "write_t4", lambda *a: ("## T4 x", 1))
    monkeypatch.setattr(analyze, "write_t3", lambda *a: ("## T3 x", 1, 1))
    monkeypatch.setattr(analyze, "write_t6", lambda *a: ("## T6a x\n## T6b x", 4, 2))
    text, counts, plots = analyze.write_all(tmp_path, {}, {}, tmp_path)
    heads = [line for line in text.splitlines() if line.startswith("## ")]
    assert heads == ["## T6a x", "## T6b x", "## T1 x", "## T3 x", "## T4 x", "## T5 x"]
    assert "3 PIDE cells" in counts and "2 T1 cells" in counts and plots == 4
