"""Training metrics stage 3b: the T5 W = 1 cell on a fake server — seed
attempts classified (review 7bedac5 S1), a kept seed rolled back (S2), the
larger budgets only for seeds that looped, the source hash, the corpus block
after the seeds, and the summary."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(
    0, str(Path(__file__).resolve().parents[1] / "evaluation" / "training_metrics")
)

from trainmetrics import corpus, metrics_t5, replay, run_t5, t5_core  # noqa: E402
from trainmetrics.replay import BlockRecord  # noqa: E402


@pytest.fixture(autouse=True)
def _no_settle_pause(monkeypatch):
    """The fake server has no prover to sample: no 1.5 s pause per attempt."""
    monkeypatch.setattr(t5_core, "SETTLE_S", 0.0)


THY = """theory Seeded
  imports Main
begin

definition double :: "nat \\<Rightarrow> nat" where "double n = n + n"

lemma double_add [simp]: "double (a + b) = double a + double b"
  by (simp add: double_def)

lemma
  fixes x :: nat
  assumes "x > 0"
  shows "x + 1 > 1"
  using assms by simp

lemma no_plus: "rev (rev xs) = xs"
  by simp

lemma other: "length (xs @ ys) = length ys + length xs"
  by simp

theorem plus_cart: \\<open>(a::nat) + 0 = a\\<close> by simp

end
"""


class FakeServer:
    """subst seeds loop (timed_out); metis seeds are kept (an open proof) or
    fail, per ``metis``; corpus blocks prove. The document is the list of
    accepted chunks; rollback drops the last."""

    def __init__(self, metis="kept"):
        self.metis = metis
        self.docs = {}
        self.closed = []
        self.rollbacks = 0
        self.n = 0
        self.mem = 0.0

    async def acquire_session(self, **kw):
        assert kw["reuse_dirty"] is False
        self.n += 1
        sid = f"s{self.n}"
        self.docs[sid] = []
        return {"session_id": sid, "lease_id": f"l{self.n}", "reused": False}

    async def enter_theory(self, sid, name, *, imports=None, lease_id=None):
        self.docs[sid].append(f"theory {name}")
        return {"success": True}

    async def verify_chunk(self, sid, chunk, timeout=None, *, lease_id=None):
        await asyncio.sleep(0)
        if "subst add.commute" in chunk:
            return {
                "success": False,
                "timed_out": True,
                "stuck_line": 2,
                "commands": [{"status": "ok"}, {"status": "running"}],
            }
        if "metis" in chunk and self.metis == "failed":
            return {
                "success": False,
                "timed_out": False,
                "commands": [
                    {"status": "ok"},
                    {
                        "status": "failed",
                        "messages": [
                            {
                                "sev": "error",
                                "text": "Failed to apply initial proof method",
                            }
                        ],
                    },
                ],
            }
        if "metis" in chunk:  # kept: an open proof stays in the document
            self.docs[sid].append(chunk)
            return {
                "success": True,
                "proof_open": True,
                "timed_out": False,
                "commands": [{"status": "ok"}, {"status": "ok"}],
            }
        self.docs[sid].append(chunk)
        return {"success": True, "timed_out": False, "commands": [{"status": "ok"}]}

    async def get_source(self, sid, lease_id=None):
        return {"source": "\n".join(self.docs[sid]), "theory": "T"}

    async def rollback(self, sid, lease_id=None):
        self.rollbacks += 1
        self.docs[sid].pop()
        return {"success": True}

    async def close_session(self, sid, lease_id=None):
        self.closed.append(sid)
        return {}

    async def memory_status(self):
        self.mem += 100.0
        rows = [
            {
                "session_id": sid,
                "rss_mb": self.mem,
                "heap_mb": 1.0,
                "mem_sampled_at": 1e12,
            }
            for sid in self.docs
        ]
        return {"state": "ok", "memory": {"used_mb": 1000.0}, "sessions": rows}


def _setup(tmp_path):
    path = tmp_path / "Seeded.thy"
    path.write_text(THY)
    cf = corpus.load_file(path)
    census = {
        "census_id": "cid",
        "files": [
            {
                "stem": cf.stem,
                "blocks": [
                    {"index": i, "proved": True, "n_commands": 2, "wall_s": 0.5}
                    for i in range(len(cf.blocks))
                ],
            }
        ],
    }
    t5 = {"seeds_per_family": 2, "max_files": 1, "budgets": {5: 2, 30: 1}}
    return cf, census, t5


def test_classify_the_seed_outcomes():
    def rec(**kw):
        return BlockRecord("s", 0, "x", kw.pop("n", 2), None, 5.0, "override", **kw)

    assert t5_core.classify(rec(timed_out=True)) == "timed_out"
    assert t5_core.classify(rec(n=None, error="ConnectError")) == "error"
    assert t5_core.classify(rec(success=True, proof_open=True)) == "kept"
    assert t5_core.classify(rec()) == "failed"


def test_a_w1_cell_on_a_fake_server(tmp_path):
    cf, census, t5 = _setup(tmp_path)
    srv = FakeServer(metis="kept")
    out = tmp_path / "cell"
    body = asyncio.run(
        run_t5.run_cell(srv, t5, [cf], census, heap="HOL", seed=1, out_dir=out)
    )
    assert body["outcome"] == "ok" and body["seeds"] == 4 and srv.closed == ["s1"]
    files = [json.loads(x) for x in (out / "positions.jsonl").read_text().splitlines()]
    assert len(json.loads((out / "seeds.json").read_text())) == 4
    (f,) = files
    atts = metrics_t5.attempts(files)
    subst = [a for a in atts if a["family"] == "subst"]
    metis = [a for a in atts if a["family"] == "metis"]
    # both subst seeds at 5 s; the first that looped also at 30 s (one per family)
    assert sorted(a["budget_s"] for a in subst) == [5.0, 5.0, 30.0]
    assert all(a["outcome"] == "timed_out" and a["undo_s"] is not None for a in subst)
    # kept metis seeds: rolled back, never tried at 30 s, the document as before
    assert [a["budget_s"] for a in metis] == [5.0, 5.0] and srv.rollbacks == 2
    assert all(a["outcome"] == "kept" and a["rolled_back"] for a in metis)
    assert all(a["source_unchanged"] for a in atts)
    assert all(a["t_settled"] >= a["t_start"] + a["wall_s"] for a in atts)  # 1676d29 B1
    assert all(
        p["verdict_match"] and p["recovery_s"] is not None for p in f["positions"]
    )
    last = max(p["position"] for p in f["positions"])
    assert f["prefix_blocks"] == last + 1 - len(f["positions"])
    s = body["summary"]
    assert s["positions"] == 4 and s["families"]["metis"]["not_looping"] == 2
    assert s["families"]["metis"]["not_looping_outcomes"]["kept"] == 2
    assert s["families"]["subst"]["not_looping"] == 0
    row = next(r for r in s["table"] if r["family"] == "subst" and r["budget_s"] == 5.0)
    assert row["timed_out_frac"] == 1.0 and row["stuck_line_frac"] == 1.0
    assert row["source_unchanged_frac"] == 1.0


def test_a_kept_seed_left_in_place_shows_as_a_changed_source(tmp_path):
    """Without the rollback the hash check would catch it (S2's guard is real)."""
    cf, census, t5 = _setup(tmp_path)

    class NoRollback(FakeServer):
        async def rollback(self, sid, lease_id=None):
            raise RuntimeError("no rollback")

    srv = NoRollback(metis="kept")
    body = asyncio.run(
        run_t5.run_cell(
            srv, t5, [cf], census, heap="HOL", seed=1, out_dir=tmp_path / "c"
        )
    )
    metis = [
        a
        for a in metrics_t5.attempts(
            [
                json.loads(x)
                for x in (tmp_path / "c" / "positions.jsonl").read_text().splitlines()
            ]
        )
        if a["family"] == "metis"
    ]
    assert all(not a["rolled_back"] and a["source_unchanged"] is False for a in metis)
    assert all("rollback failed" in a["error"] for a in metis)
    assert body["summary"]["table"]


def test_failed_seeds_and_a_refused_acquisition(tmp_path):
    cf, census, t5 = _setup(tmp_path)
    body = asyncio.run(
        run_t5.run_cell(
            FakeServer(metis="failed"),
            t5,
            [cf],
            census,
            heap="HOL",
            seed=1,
            out_dir=tmp_path / "c",
        )
    )
    fam = body["summary"]["families"]["metis"]
    assert (
        fam["not_looping_outcomes"]["failed"] == 2 and fam["verdict_match_frac"] == 1.0
    )

    class Refusing(FakeServer):
        async def acquire_session(self, **kw):
            raise RuntimeError("503 gateway_unavailable")

    body = asyncio.run(
        run_t5.run_cell(
            Refusing(), t5, [cf], census, heap="HOL", seed=1, out_dir=tmp_path / "d"
        )
    )
    assert body["summary"]["acquire_errors"] == 1 and body["summary"]["positions"] == 0


def test_parse_budgets():
    assert run_t5.parse_budgets("5:2, 30:1") == {5.0: 2, 30.0: 1}


class _Gone(Exception):
    def __init__(self):
        super().__init__("404 Not Found")
        self.response = type("R", (), {"status_code": 404})()


def test_a_dead_prover_stops_the_file_and_is_counted_apart(tmp_path):
    """Review beac25c S1 / plan_v1.6 B1: no failed rollouts from a dead session."""
    cf, census, t5 = _setup(tmp_path)

    class Dying(FakeServer):
        dead = False

        async def verify_chunk(self, sid, chunk, timeout=None, *, lease_id=None):
            if self.dead:
                raise _Gone()
            if "metis" in chunk:
                self.dead = True
                return {
                    "success": False,
                    "timed_out": False,
                    "error_kind": "prover_died",
                    "commands": [{"status": "failed"}],
                }
            return await super().verify_chunk(sid, chunk, timeout, lease_id=lease_id)

        async def close_session(self, sid, lease_id=None):
            raise _Gone()  # already gone: close_all must not wait for it

    out = tmp_path / "c"
    body = asyncio.run(
        run_t5.run_cell(Dying(), t5, [cf], census, heap="HOL", seed=1, out_dir=out)
    )
    s = body["summary"]
    assert body["stopped_files"] == {"Seeded": "prover_died"}
    assert (
        body["unclosed_sessions"] == [] and s["stopped_files"] == body["stopped_files"]
    )
    metis = s["families"]["metis"]
    assert metis["ended_apart"]["prover_died"] == 1 and metis["not_looping"] == 0
    for r in s["table"]:
        assert r["attempts"] == r["timed_out"] + r["failed"] + r["kept"] + r["error"]
    files = [json.loads(x) for x in (out / "positions.jsonl").read_text().splitlines()]
    assert metrics_t5.attempts(files)[-1]["outcome"] == "prover_died"  # nothing after


def test_why_a_seed_failed_and_the_memory_it_left_are_recorded(tmp_path):
    """Review beac25c S2 / S5."""
    cf, census, t5 = _setup(tmp_path)
    out = tmp_path / "c"
    asyncio.run(
        run_t5.run_cell(
            FakeServer(metis="failed"),
            t5,
            [cf],
            census,
            heap="HOL",
            seed=1,
            out_dir=out,
        )
    )
    files = [json.loads(x) for x in (out / "positions.jsonl").read_text().splitlines()]
    metis = [a for a in metrics_t5.attempts(files) if a["family"] == "metis"]
    assert all(
        a["first_error"] == "Failed to apply initial proof method" for a in metis
    )
    assert all(a["mem_after"]["state"] == "ok" for a in metis)  # the server tier
    summary = metrics_t5.summarize(files)
    fam = summary["families"]["metis"]
    assert fam["not_looping_errors"] == ["Failed to apply initial proof method"]
    row = next(r for r in summary["table"] if r["family"] == "metis")
    assert row["tiers_after"] == ["ok"] and row["retained_max_mb"] is None  # no sampler


def _sample(t, provers):
    procs = [
        {"pid": pid, "role": "poly", "rss": int(mb * 2**20), "cpu_ticks": ticks}
        for pid, (mb, ticks) in provers.items()
    ]
    return {
        "t": t,
        "procs": procs + [{"pid": 1, "role": "jvm", "rss": 1, "cpu_ticks": 9}],
    }


def test_the_prover_that_ran_the_attempt_and_its_retained_memory():
    """Review beac25c S2 / 1676d29 B1: the busy prover's RSS before, at peak and
    in the quiet pause after the call; an idle prover beside it (a leftover
    session) is not picked; a row after the pause (the next call, the session
    closing) is not "after"."""
    samples = [
        _sample(100.0, {10: (1000, 0), 20: (3000, 50)}),
        _sample(101.0, {10: (1500, 100), 20: (3000, 50)}),
        _sample(102.0, {10: (4000, 200), 20: (3000, 50)}),
        _sample(103.0, {10: (3500, 210), 20: (3000, 50)}),  # in the pause
        _sample(104.0, {10: (900, 260), 20: (3000, 50)}),  # the session closing
        _sample(105.0, {20: (3000, 50)}),  # gone
    ]
    m = metrics_t5.prover_memory(samples, 100.5, 102.5, 103.8)
    assert m["pid"] == 10 and m["rss_before_mb"] == 1000.0 and m["censored"] is None
    assert m["rss_peak_mb"] == 4000.0 and m["rss_after_mb"] == 3500.0
    assert m["retained_mb"] == 2500.0
    assert (
        metrics_t5.prover_memory(samples, 99.0, 100.5, 101.0) is None
    )  # nothing before
    # the reviewer's live case: no row in the pause, the next one sees the close
    m = metrics_t5.prover_memory(samples, 100.5, 103.2, 103.9)
    assert m["censored"] == "no_row_in_pause" and m["retained_mb"] is None
    # the prover gone in the pause's row
    m = metrics_t5.prover_memory(samples, 100.5, 104.5, 105.5)
    assert m["censored"] == "prover_gone" and m["retained_mb"] is None
    # a record without a pause (older code) is censored, not guessed
    assert metrics_t5.prover_memory(samples, 100.5, 102.5)["censored"] == "no_pause"
    idle = [_sample(t, {20: (3000, 50)}) for t in (100.0, 101.0, 102.0, 103.0)]
    assert metrics_t5.prover_memory(idle, 100.5, 102.5, 103.5) is None  # nobody worked
    att = {"seed_id": "subst-0", "family": "subst", "budget_s": 1.0}
    att.update({"outcome": "timed_out", "wall_s": 2.0, "t_start": 100.5})
    late = dict(att, seed_id="subst-1", t_start=100.5, wall_s=2.7, t_settled=103.9)
    files = [
        {
            "stem": "S",
            "positions": [
                {"position": 1, "attempts": [dict(att, t_settled=103.8)]},
                {"position": 2, "attempts": [late]},
            ],
        }
    ]
    metrics_t5.annotate(files, samples)
    row = metrics_t5.summarize(files)["table"][0]
    assert row["retained_max_mb"] == 2500.0 and row["retained_p50_mb"] == 2500.0
    assert row["retained_censored"] == 1 and row["peak_growth_max_mb"] == 3000.0


def test_a_memory_error_is_its_own_class():
    rec = BlockRecord("s", 0, "x", 2, None, 5.0, "override", error_kind="memory")
    assert t5_core.classify(rec) == "memory"
    gone = BlockRecord(
        "s", 0, "x", None, None, 5.0, "override", error_kind="session_gone"
    )
    assert t5_core.classify(gone) == "session_gone"


def test_verify_block_marks_a_gone_session_and_keeps_the_error_kind():
    class Gone:
        async def verify_chunk(self, sid, chunk, timeout=None, *, lease_id=None):
            raise _Gone()

    class Memory:
        async def verify_chunk(self, sid, chunk, timeout=None, *, lease_id=None):
            msgs = [
                {"sev": "warning", "text": "w"},
                {"sev": "error", "text": "Out of memory"},
            ]
            return {
                "success": False,
                "error_kind": "memory",
                "commands": [{"status": "failed", "messages": msgs}],
            }

    sess = replay.SessionRef("s1", "l1")
    kw = {"stem": "S", "index": 0, "block_sha": "h", "census_commands": 1}
    rec = asyncio.run(replay.verify_block(Gone(), sess, "x", **kw))
    assert rec.error_kind == "session_gone" and rec.n_commands is None
    rec = asyncio.run(replay.verify_block(Memory(), sess, "x", **kw))
    assert rec.error_kind == "memory" and rec.first_error == "Out of memory"


def test_an_empty_cell_is_not_ok(tmp_path):
    cf, census, t5 = _setup(tmp_path)

    class Refusing(FakeServer):
        async def acquire_session(self, **kw):
            raise RuntimeError("503 gateway_unavailable")

    body = asyncio.run(
        run_t5.run_cell(
            Refusing(), t5, [cf], census, heap="HOL", seed=1, out_dir=tmp_path / "e"
        )
    )
    assert body["outcome"] == "empty"
