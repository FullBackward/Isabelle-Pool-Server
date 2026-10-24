"""Training metrics stage 8a: T1's PIDE MCP row — get_state counts, a cell on
a fake PIDE MCP (a scratch per file, one edit per block, settled at the
first idle reading after the count grew, T1-format records), a failed block,
a block that never settles ending its file, the matrix plan, and the row
beside IPS in the analysis."""

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
    def __init__(self):
        self.mcp = FakeMCP()
        self.session_ids = {}

    async def start_session(self, i):
        self.session_ids[i] = f"s{i}"

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
    assert (
        body["outcome"] == "ok"
        and body["poll_s"] == 0.0
        and sorted(body["session_start_s"]) == [0, 1]
    )
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
