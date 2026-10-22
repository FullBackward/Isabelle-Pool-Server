"""Training metrics, stage 1 (plan 2026-10-22 v1.1): corpus split and seeded
assignment, the budget rule, unique theory names per session (plan review
B1), the replay records and the census, against a fake client."""

from __future__ import annotations

import asyncio
import json
import sys
from collections import Counter
from pathlib import Path

import pytest

sys.path.insert(
    0, str(Path(__file__).resolve().parents[1] / "evaluation" / "training_metrics")
)

from trainmetrics import census, corpus, replay  # noqa: E402

THY_A = """theory A
  imports "HOL-Analysis.Abstract_Topology"
begin

lemma a1: "True" by simp

lemma a2: "x = x"
proof -
  show ?thesis by simp
qed

definition f :: "nat => nat" where "f n = n"

end
"""

THY_B = """theory B imports Main begin

lemma b1: "True" by simp

end
"""


@pytest.fixture
def corpus_dir(tmp_path):
    (tmp_path / "A.thy").write_text(THY_A)
    (tmp_path / "B.thy").write_text(THY_B)
    (tmp_path / "Broken.thy").write_text("no header here\n")
    return tmp_path


def test_corpus_split_and_checksums(corpus_dir):
    files = corpus.load_corpus(str(corpus_dir))
    assert [f.stem for f in files] == ["A", "B"]  # Broken has no header: skipped
    a = files[0]
    assert a.imports == ["HOL-Analysis.Abstract_Topology"] and len(a.blocks) == 3
    assert a.blocks[1].startswith("lemma a2") and a.blocks[1].rstrip().endswith("qed")
    assert len(a.block_sha) == 3 and len(a.sha256) == 64
    assert corpus.load_corpus(str(corpus_dir), only=["B"])[0].stem == "B"
    assert corpus.corpus_sha(files) == corpus.corpus_sha(
        corpus.load_corpus(str(corpus_dir))
    )


def test_load_paths_and_the_local_smoke_files_split():
    root = Path(__file__).resolve().parents[1]
    files = corpus.load_paths(
        [
            "evaluation/memory_scaling/workloads/CTL.thy",
            "evaluation/Server_Concurrency.thy",
            "examples/mcp_lsp_demo.thy",
        ]
    )
    assert [f.stem for f in files] == ["CTL", "Server_Concurrency", "mcp_lsp_demo"]
    assert all(f.imports == ["Main"] and f.blocks for f in files)
    with pytest.raises(FileNotFoundError):  # a missing smoke file fails loudly
        corpus.load_paths([str(root / "nonexistent.thy")])


def test_assignment_is_seeded_nested_and_balanced():
    stems = [f"T{i:02d}" for i in range(94)]
    a1 = corpus.assignment(stems, 7, 1, 8)
    a8 = corpus.assignment(stems, 7, 8, 8)
    a32 = corpus.assignment(stems, 7, 32, 8)
    assert len(a8) == 8 and all(len(w) == 8 for w in a8)
    assert corpus.assignment(stems, 7, 8, 8) == a8  # repeatable
    flat8 = [s for i in range(8) for w in a8 for s in w[i : i + 1]]
    flat32 = [s for i in range(8) for w in a32 for s in w[i : i + 1]]
    assert flat32[: len(flat8)] == flat8  # a smaller run is a prefix of a larger one
    assert a1[0] == flat8[:8]
    counts = Counter(flat32)  # 256 replays over 94 files: each 2 or 3 times
    assert set(counts.values()) <= {2, 3} and len(counts) == 94
    assert corpus.assignment(stems, 8, 8, 8) != a8
    with pytest.raises(ValueError):
        corpus.assignment([], 7, 1, 1)


def test_budget_rule():
    assert replay.budget_for(None) == 120.0
    assert replay.budget_for(1) == 30.0 and replay.budget_for(15) == 30.0
    assert replay.budget_for(40) == 80.0 and replay.budget_for(1000) == 600.0


class FakeClient:
    """Fails like the server would mislead: a reused theory name on a session
    would append to the old node, so the fake refuses it outright."""

    def __init__(self, fail_block=None, header_error=None):
        self.names = {}
        self.calls = []
        self.fail_block = fail_block
        self.header_error = header_error
        self.closed = []

    async def acquire_session(self, **kw):
        self.calls.append(("acquire", kw))
        return {"session_id": "s1", "lease_id": "l1"}

    async def close_session(self, sid, lease_id=None):
        self.closed.append(sid)
        return {}

    async def enter_theory(self, sid, name, *, imports=None, lease_id=None):
        used = self.names.setdefault(sid, set())
        if name in used:
            raise AssertionError(f"theory name {name} reused on {sid}")
        used.add(name)
        self.calls.append(("enter", name, tuple(imports or ())))
        if self.header_error:
            return {"success": False, "error": self.header_error}
        return {"success": True}

    async def verify_chunk(self, sid, chunk, timeout=None, *, lease_id=None):
        self.calls.append(("verify", chunk.split()[1], timeout))
        n = max(1, chunk.count("\n") + 1)
        if self.fail_block and self.fail_block in chunk:
            return {
                "success": False,
                "proof_open": False,
                "used_sorry": False,
                "timed_out": True,
                "stuck_line": 1,
                "commands": [{"status": "running"}] * n,
                "execution_time": timeout,
            }
        return {
            "success": True,
            "proof_open": False,
            "used_sorry": "sorry" in chunk,
            "timed_out": False,
            "commands": [{"status": "ok"}] * n,
            "execution_time": 0.01,
        }


def test_replay_uses_a_new_name_per_replay_and_records_blocks(corpus_dir):
    files = corpus.load_corpus(str(corpus_dir))
    cl = FakeClient(fail_block="a2")
    sess = replay.SessionRef("s1", "l1", worker=3)

    async def go():
        r1 = await replay.replay_file(cl, sess, files[0], [1, 4, 1])
        r2 = await replay.replay_file(cl, sess, files[0])  # same file again
        return r1, r2

    r1, r2 = asyncio.run(go())
    assert r1.theory_name == "A__w3_r0" and r2.theory_name == "A__w3_r1"
    assert r2.nodes_on_session == 2 and sess.nodes == 2
    b = r1.blocks
    assert [x.proved for x in b] == [True, False, True]
    assert b[1].timed_out and b[1].stuck_line == 1 and b[1].statuses == {"running": 4}
    assert b[0].budget_s == 30.0 and b[0].budget_source == "census"
    assert r2.blocks[0].budget_source == "none" and r2.blocks[0].budget_s == 120.0
    assert b[0].n_commands == 1 and b[1].n_commands == 4
    json.dumps(r1.as_dict())


def test_failed_header_stops_the_replay(corpus_dir):
    files = corpus.load_corpus(str(corpus_dir))
    cl = FakeClient(header_error="Bad theory import")
    rr = asyncio.run(replay.replay_file(cl, replay.SessionRef("s1", "l1"), files[1]))
    assert rr.enter_error == "Bad theory import" and rr.blocks == []


def test_is_proved_definition():
    base = {"success": True, "proof_open": False, "used_sorry": False}
    assert replay.is_proved(base)
    for key, val in (
        ("success", False),
        ("proof_open", True),
        ("used_sorry", True),
        ("timed_out", True),
    ):
        assert not replay.is_proved({**base, key: val})


def test_census_end_to_end(corpus_dir, tmp_path):
    files = corpus.load_corpus(str(corpus_dir))
    cl = FakeClient()
    records = asyncio.run(
        census.run_census(cl, files, field="HOL-Analysis", session_theories=["Main"])
    )
    acquire = cl.calls[0][1]
    assert acquire["field"] == "HOL-Analysis" and acquire["reuse_dirty"] is False
    assert all(c[2] == replay.BUDGET_MAX_S for c in cl.calls if c[0] == "verify")
    assert cl.closed == ["s1"]
    body = census.write_census(tmp_path / "out", files, records, {"url": "x"})
    on_disk = census.load_census(str(tmp_path / "out" / "census.json"))
    assert on_disk["census_id"] == body["census_id"] and len(body["census_id"]) == 64
    assert on_disk["totals"]["files"] == 2 and on_disk["totals"]["blocks"] == 4
    assert on_disk["totals"]["proved_blocks"] == 4
    counts = census.counts_by_stem(on_disk)
    assert counts["A"] == [b["n_commands"] for b in on_disk["files"][0]["blocks"]]
    assert (tmp_path / "out" / "replays.jsonl").read_text().count("\n") == 2
    # the id changes when a count changes
    on_disk["files"][0]["blocks"][0]["n_commands"] += 1
    assert census.census_id(on_disk["files"]) != body["census_id"]


def test_partial_census_counts_get_the_maximum_budget(corpus_dir, tmp_path):
    """Review 86afb15 S3: a block that timed out in the census has a partial
    count; census.json flags it, and a later replay gives it the maximum."""
    assert replay.budget_for(3, partial=True) == replay.BUDGET_MAX_S
    files = corpus.load_corpus(str(corpus_dir))
    cl = FakeClient(fail_block="a2")  # block A[1] times out
    records = asyncio.run(
        census.run_census(cl, files, field="HOL", session_theories=["Main"])
    )
    body = census.write_census(tmp_path / "c", files, records, {})
    a_rows = body["files"][0]["blocks"]
    assert [b["count_partial"] for b in a_rows] == [False, True, False]
    assert body["totals"]["partial_count_blocks"] == 1
    partial = census.partial_by_stem(body)
    assert partial["A"] == [False, True, False] and partial["B"] == [False]
    rr = asyncio.run(
        replay.replay_file(
            FakeClient(),
            replay.SessionRef("s2", "l2"),
            files[0],
            census.counts_by_stem(body)["A"],
            census_partial=partial["A"],
        )
    )
    sources = [(b.budget_source, b.budget_s) for b in rr.blocks]
    assert sources[1] == ("census_partial", replay.BUDGET_MAX_S)
    assert sources[0][0] == "census" and sources[2][0] == "census"
