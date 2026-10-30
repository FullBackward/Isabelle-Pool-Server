"""Training metrics stage 8a: T1's PIDE MCP row — get_state counts, a cell on
a fake PIDE MCP (a scratch per file, one edit per block, settled at the
first idle reading after the count grew, T1-format records), a failed block,
a block that never settles ending its file, the matrix plan, the row
beside IPS in the analysis, and a session per file (D18)."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(
    0, str(Path(__file__).resolve().parents[1] / "evaluation" / "training_metrics")
)

from trainmetrics import (  # noqa: E402
    analyze_pide,
    corpus,
    drive_pide_chunks,
    metrics_t12,
    run_matrix,
)


def _thy(name, n=4):
    lemmas = "\n\n".join(
        f'lemma {name}_{k}: "x{k} = x{k}"\n  by simp' for k in range(n)
    )
    return f"theory {name}\n  imports Main\nbegin\n\n{lemmas}\n\nend\n"


class FakeMCP:
    """Counts follow the text: 2 per command (PIDE counts the spans between
    commands too); the first reading after an edit is busy; ``FAIL`` marks a
    failed command, ``LOOP`` one that never finishes."""

    def __init__(self):
        self.texts, self.fresh, self.n = {}, set(), 0

    async def call(self, tool, args, timeout_s=None):
        # a real call takes time: without it a block's wall_s (4 decimals) can
        # round to 0 on a fast machine and the rate reads 0
        await asyncio.sleep(0.001)
        if tool == "create_scratch":
            self.n += 1
            return {"path": f"/tmp/Scratch_{self.n}.thy"}
        if tool == "edit":
            self.texts[args["origin"]] = args["text"]
            self.fresh.add(args["origin"])
            return {"ok": True}
        assert tool == "get_state"
        text = self.texts.get(args["origin"], "")
        cmds = sum(1 for line in text.splitlines() if line and not line.startswith(" "))
        cmds += sum(1 for line in text.splitlines() if line.strip().startswith("by"))
        busy = 1 if args["origin"] in self.fresh or "LOOP" in text else 0
        self.fresh.discard(args["origin"])
        return {
            "commands": {"count": 2 * cmds, "count_returned": 0, "commands": []},
            "commands_unprocessed": busy,
            "commands_failed": {"count": text.count("FAIL"), "commands": []},
            "commands_bad": {"count": 0, "commands": []},
            "errors": {"count": 0, "commands": []},
            "total_timing_ms": 10 * cmds,
        }


class FakePIDE:
    def __init__(self, fail_start=()):
        self.mcp = FakeMCP()
        self.session_ids = {}
        self.fail_start, self.log = set(fail_start), []

    async def start_session(self, i, name=None):
        await asyncio.sleep(0.001)  # a start takes time (acquire_s > 0)
        name = name or f"s{i}"
        self.log.append(("start", name))
        if name in self.fail_start:
            raise RuntimeError(f"cannot start {name}")
        self.session_ids[i] = name

    async def stop_session(self, i):
        self.log.append(("stop", self.session_ids.pop(i)))

    def _mcp(self, i):
        return self.mcp

    def session_options(self):
        return {"isabelle_options": "defaults"}


def _setup(tmp_path, names=("Ta", "Tb")):
    files = []
    for n in names:
        p = tmp_path / f"{n}.thy"
        p.write_text(_thy(n))
        files.append(corpus.load_file(p))
    census = {
        "census_id": "cid",
        "files": [
            {
                "stem": cf.stem,
                "blocks": [
                    {"index": i, "proved": True, "n_commands": 2, "wall_s": 0.1}
                    for i in range(len(cf.blocks))
                ],
            }
            for cf in files
        ],
        "totals": {},
    }
    return files, census


def _cell(tmp_path, files, census, workers=2, name="c"):
    return asyncio.run(
        drive_pide_chunks.run_cell(
            FakePIDE(),
            {"poll_s": 0.0},
            files,
            census,
            workers,
            1,
            heap="HOL",
            seed=3,
            out_dir=tmp_path / name,
        )
    )


def _replays(tmp_path, name="c"):
    return [
        json.loads(x)
        for x in (tmp_path / name / "replays.jsonl").read_text().splitlines()
    ]


def test_counts_from_get_state():
    state = {
        "commands": {"count": 12, "commands": []},
        "commands_unprocessed": 0,
        "commands_failed": {"count": 1},
        "total_timing_ms": 40,
        "flag": True,
    }
    assert drive_pide_chunks.counts(state) == {
        "commands": 12,
        "commands_unprocessed": 0,
        "commands_failed": 1,
        "total_timing_ms": 40,
    }


def test_a_pide_cell_writes_t1_records(tmp_path):
    files, census = _setup(tmp_path)
    body = _cell(tmp_path, files, census)
    assert body["outcome"] == "ok" and body["poll_s"] == 0.0
    rr = _replays(tmp_path)
    assert sorted(r["stem"] for r in rr) == ["Ta", "Tb"] and all(
        r["theory_name"] for r in rr
    )
    for r in rr:
        assert len(r["blocks"]) == 4 and all(b["proved"] for b in r["blocks"])
        # n_commands is the census count (IPS's unit); PIDE's own delta beside it
        assert all(
            b["n_commands"] == 2 and b["statuses"]["pide_commands_delta"] == 4
            for b in r["blocks"]
        )
        assert all(
            b["statuses"]["polls"] == 2 for b in r["blocks"]
        )  # busy, then settled
    row = metrics_t12.summarize_cell(
        {**body, "_replays": rr, "_samples_path": "none"}, census
    )
    assert row["chunks_per_min"] > 0 and row["timeout_rate"] == 0


def test_a_failed_block_and_one_that_never_settles(tmp_path, monkeypatch):
    files, census = _setup(tmp_path, names=("Ta",))
    cf = files[0]
    cf.blocks[1] = cf.blocks[1].replace("by simp", "by simp FAIL")
    cf.blocks[2] = cf.blocks[2].replace("by simp", "by LOOP")
    monkeypatch.setattr(drive_pide_chunks, "budget_for", lambda n, partial=False: 0.05)
    _cell(tmp_path, files, census, workers=1)
    blocks = _replays(tmp_path)[0]["blocks"]
    assert [b["proved"] for b in blocks] == [True, False, False]
    assert blocks[1]["statuses"]["bad"] == 1 and not blocks[1]["timed_out"]
    assert blocks[2]["timed_out"]  # it never settled: the file ends there (no block 3)


def test_the_matrix_plans_the_pide_row():
    cfg = {
        "workers": [1, 2, 4],
        "repeats": 3,
        "pide": {"workers": [8, 1], "repeats": 2},
    }
    assert list(run_matrix.cells(cfg, experiment="pide")) == [
        (1, 0),
        (1, 1),
        (8, 0),
        (8, 1),
    ]
    ns = run_matrix.argparse.Namespace(
        experiment="pide",
        config="c",
        census="x.json",
        out="o",
        local_smoke=True,
        ips_external=True,
        url="http://h:1",
        quota=2,
    )
    cmd = run_matrix.child_cmd(ns, 8, 1)
    assert cmd[3] == "trainmetrics.drive_pide_chunks" and cmd[-2:] == ["--quota", "2"]
    assert (
        "--ips-external" not in cmd and "--url" not in cmd
    )  # it starts its own MCP process
    assert run_matrix.cell_path(Path("o"), 8, 1, "pide") == Path("o/pide/w8/rep1")


def test_the_pide_row_beside_ips(tmp_path):
    files, census = _setup(tmp_path)
    root = tmp_path / "results"
    out = drive_pide_chunks.cell_dir(root, 2, 0)
    body = asyncio.run(
        drive_pide_chunks.run_cell(
            FakePIDE(),
            {"poll_s": 0.0},
            files,
            census,
            2,
            1,
            heap="HOL",
            seed=3,
            out_dir=out,
        )
    )
    (out / "manifest.json").write_text(json.dumps({**body, "rep": 0}))
    (root / "tables").mkdir()
    ips = [
        {
            "workers": 2,
            "chunks_per_min": 10.0,
            "chunks_per_min_incl_setup": 5.0,
            "census_work_rate": 1.0,
            "latency_p50_s": 0.2,
            "latency_p95_s": 0.4,
            "timeout_rate": 0.0,
        }
    ]
    env = {"ISABELLE_PARALLEL_PROOFS": "2"}
    text, n = analyze_pide.write_pide(root, census, root / "tables", ips, env)
    # review 9ad8799 S1: both rows' proof-checking settings are stated
    assert "IPS `parallel_proofs=2`" in text and "PIDE MCP sessions" in text
    assert n == 1 and "## T1: the PIDE MCP row" in text
    assert "| 2 | IPS | 10 |" in text and "| 2 | PIDE MCP A |" in text
    assert (root / "tables" / "t1_pide_series.csv").exists()
    assert "t1_pide_latency" not in text  # no IPS cell on disk: nothing to pair
    # D20: IPS cells with the same blocks pair with the PIDE cell, by block
    pide_cells = metrics_t12.load_t1_cells(root, "pide")
    ips_cells = [
        {
            "workers": 2,
            "outcome": "ok",
            "_replays": [
                {"stem": rr["stem"], "blocks": [{**b, "wall_s": 0.25} for b in rr["blocks"]]}
                for rr in pide_cells[0]["_replays"]
            ],
        }
    ]
    text, _n = analyze_pide.write_pide(root, census, root / "tables", ips, env, ips_cells)
    assert "| 2 | all | 8 |" in text and "| 2 | 2-9 | 8 |" in text
    assert (root / "tables" / "t1_pide_latency.csv").exists()


def test_the_pide_sessions_get_the_options():
    """D12: start_session carries `options` (parallel_proofs=2, IPS's)."""
    sys.path.insert(
        0, str(Path(__file__).resolve().parents[1] / "evaluation" / "memory_scaling")
    )
    from memscale.drive_pide import (  # pylint: disable=import-outside-toplevel
        PIDEDriver,
    )

    calls = []

    class Rec:
        async def call(self, tool, args, timeout_s=None):
            calls.append((tool, args))
            return {"session": args["session"]}

    d = PIDEDriver(heap="HOL", topology="A", k=1, session_options=["parallel_proofs=2"])
    d.procs = [Rec()]
    asyncio.run(d.start_session(0))
    assert calls == [
        (
            "start_session",
            {
                "logic": "HOL",
                "session": "s0",
                "no_build": True,
                "options": ["parallel_proofs=2"],
            },
        )
    ]
    assert d.session_options()["isabelle_options"] == ["parallel_proofs=2"]
    plain = PIDEDriver(heap="HOL", topology="A", k=1)
    plain.procs = [Rec()]
    asyncio.run(plain.start_session(0))
    assert "options" not in calls[-1][1]  # memscale's sessions: unchanged


def test_the_scratch_header_quotes_every_import():
    """The server smoke of 2026-10-25: `imports HOL-Analysis.X` does not parse,
    so no HOL-Analysis file entered; a quoted name does (`"Main"` too)."""
    pide = FakePIDE()
    asyncio.run(pide.start_session(0))
    scratch = drive_pide_chunks.Scratch(pide, 0, 0.0)
    asyncio.run(scratch.new_file(["HOL-Analysis.Integral_Test", "Main"]))
    assert scratch.text == (
        'theory Scratch_1\n  imports "HOL-Analysis.Integral_Test" "Main"\nbegin\n'
    )


def test_each_file_runs_on_a_session_of_its_own(tmp_path):
    """D18 (owner, 2026-10-26: the rows pay the same setup): as IPS, a fresh
    session per file, started inside the window (its time is the file's
    acquire_s) and stopped after the file."""
    files, census = _setup(tmp_path, names=("Ta", "Tb", "Tc", "Td"))
    pide = FakePIDE()
    body = asyncio.run(
        drive_pide_chunks.run_cell(
            pide,
            {"poll_s": 0.0},
            files,
            census,
            2,
            2,
            heap="HOL",
            seed=3,
            out_dir=tmp_path / "c",
        )
    )
    rr = _replays(tmp_path)
    assert body["outcome"] == "ok" and body["unclosed_sessions"] == []
    assert len({r["session_id"] for r in rr}) == 4 and pide.session_ids == {}
    assert all(r["nodes_on_session"] == 1 and r["acquire_s"] > 0 for r in rr)
    assert all(r["t_start"] >= body["window_start"] for r in rr)
    for w in (0, 1):  # each worker: start, stop, start, stop
        mine = [(a, n) for a, n in pide.log if n.startswith(f"s{w}_")]
        assert [a for a, _ in mine] == ["start", "stop", "start", "stop"]
        assert [n for _, n in mine] == [f"s{w}_f1"] * 2 + [f"s{w}_f2"] * 2


def test_a_session_that_does_not_start_skips_only_its_file(tmp_path):
    files, census = _setup(tmp_path, names=("Ta", "Tb"))
    pide = FakePIDE(fail_start={"s0_f1"})
    body = asyncio.run(
        drive_pide_chunks.run_cell(
            pide,
            {"poll_s": 0.0},
            files,
            census,
            1,
            2,
            heap="HOL",
            seed=3,
            out_dir=tmp_path / "c",
        )
    )
    first, second = _replays(tmp_path)
    assert first["acquire_error"] == "RuntimeError: cannot start s0_f1"
    assert first["blocks"] == [] and len(second["blocks"]) == 4
    assert body["acquire_errors"] == 1 and body["outcome"] == "ok"
    assert ("stop", "s0_f1") not in pide.log and pide.session_ids == {}


def _cells(walls, proved=None, timed_out=None):
    """One ok W = 1 cell, one replay of ``Ta`` with these block walls."""
    proved = proved or [True] * len(walls)
    timed_out = timed_out or [False] * len(walls)
    blocks = [
        {"index": i, "block_sha": f"sha{i}", "wall_s": w, "proved": p, "timed_out": t}
        for i, (w, p, t) in enumerate(zip(walls, proved, timed_out))
    ]
    return [{"workers": 1, "outcome": "ok", "_replays": [{"stem": "Ta", "blocks": blocks}]}]


def test_the_paired_latency_pairs_the_blocks_both_rows_proved():
    """D20: a block's time per tool is the median over its repeats; a block
    proved under one tool only, or timed out in any repeat, is left out and
    counted; the sums, the share within one poll and the share IPS was the
    faster are over the pairs, per size class after the "all" row."""
    sizes = [1, 4, 12, 4]
    census = {
        "census_id": "cid",
        "files": [
            {
                "stem": "Ta",
                "blocks": [{"index": i, "n_commands": n} for i, n in enumerate(sizes)],
            }
        ],
        "totals": {},
    }
    ips = _cells([0.3, 0.5, 1.0, 0.4]) + _cells([0.5, 0.5, 1.2, 0.4])  # medians .4 .5 1.1 .4
    pide = _cells([0.2, 0.6, 1.1, 0.4], proved=[True, True, True, False])
    rows = analyze_pide.pair_latency(ips, pide, census)
    assert [(r["class"], r["pairs"]) for r in rows] == [
        ("all", 3),
        ("1", 1),
        ("2-9", 1),
        ("10+", 1),
    ]
    head = rows[0]
    assert head["blocks_in_both"] == 4 and head["dropped_one_tool_only"] == 1
    assert (head["ips_sum_s"], head["pide_sum_s"]) == (2.0, 1.9)
    assert head["ips_over_pide"] == round(2.0 / 1.9, 4)
    assert (head["ips_p50_s"], head["pide_p50_s"]) == (0.5, 0.6)
    assert head["within_one_poll"] == round(2 / 3, 4)  # +0.2, -0.1, 0.0
    assert head["ips_faster"] == round(1 / 3, 4)
    assert rows[2]["ips_mean_s"] == 0.5 and rows[3]["diff_p50_s"] == 0.0
    late = _cells([0.2, 0.6, 1.1, 0.4], timed_out=[False, False, True, False])
    rows = analyze_pide.pair_latency(ips, late, census)
    assert rows[0]["pairs"] == 3 and rows[0]["dropped_timed_out"] == 1
    assert analyze_pide.pair_latency(ips, [], census) == []  # no PIDE cell: no row
    qs = analyze_pide.latency_quantiles(ips, pide, census, step=0.25)
    assert [(r["q"], r["ips_s"], r["pide_s"]) for r in qs] == [
        (0.0, 0.4, 0.2),
        (0.25, 0.5, 0.6),
        (0.5, 0.5, 0.6),
        (0.75, 1.1, 1.1),
        (1.0, 1.1, 1.1),
    ]
