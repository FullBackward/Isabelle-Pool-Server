"""Training metrics stage 3b: the T5 W = 1 cell on a fake server — seed
attempts classified (review 7bedac5 S1), a kept seed rolled back (S2), the
larger budgets only for seeds that looped, the source hash, the corpus block
after the seeds, and the summary."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(
    0, str(Path(__file__).resolve().parents[1] / "evaluation" / "training_metrics")
)

from trainmetrics import corpus, metrics_t5, run_t5  # noqa: E402
from trainmetrics.replay import BlockRecord  # noqa: E402

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
                "commands": [{"status": "ok"}, {"status": "failed"}],
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

    assert run_t5.classify(rec(timed_out=True)) == "timed_out"
    assert run_t5.classify(rec(n=None, error="ConnectError")) == "error"
    assert run_t5.classify(rec(success=True, proof_open=True)) == "kept"
    assert run_t5.classify(rec()) == "failed"


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
