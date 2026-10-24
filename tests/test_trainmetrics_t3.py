"""Training metrics stage 5: T3 branch cost — the stratified state sample, a
cell on a fake server (create, replay, checkpoint, continue, restore,
continue again, rollback, a new node), a continuation that fails, a session
that dies, a refused create retried, the summary and the T3 snippet."""

from __future__ import annotations

import asyncio
import json
import sys
from dataclasses import asdict
from pathlib import Path

import httpx

sys.path.insert(
    0, str(Path(__file__).resolve().parents[1] / "evaluation" / "training_metrics")
)

from trainmetrics import analyze, corpus, metrics_t3, run_t3  # noqa: E402

N_BLOCKS = 9


def _thy(name):
    lemmas = "\n\n".join(
        f'lemma {name}_{k}: "x{k} = x{k}"\n  by simp' for k in range(N_BLOCKS)
    )
    return f"theory {name}\n  imports Main\nbegin\n\n{lemmas}\n\nend\n"


class Fake:
    """A document per node: a kept chunk is appended, a failed one dropped;
    a checkpoint is the node's length, a restore truncates to it, a rollback
    pops. ``fail`` chunks fail; ``die`` chunks report a dead prover."""

    def __init__(self, fail=(), die=(), refuse_creates=0):
        self.fail, self.die = set(fail), set(die)
        self.refuse = refuse_creates
        self.sessions = {}
        self.closed = []
        self.n = 0

    async def create_session(self, theories=None, field=None, label=None):
        if self.refuse:
            self.refuse -= 1
            req = httpx.Request("POST", "http://x/api/v1/sessions")
            resp = httpx.Response(
                503,
                json={"error": "pool_full"},
                headers={"Retry-After": "1"},
                request=req,
            )
            raise httpx.HTTPStatusError("503", request=req, response=resp)
        self.n += 1
        sid = f"s{self.n}"
        self.sessions[sid] = {"nodes": {}, "cur": None, "cps": {}}
        return {"session_id": sid, "lease_id": "l"}

    async def enter_theory(self, sid, name, *, imports=None, lease_id=None):
        s = self.sessions[sid]
        assert name not in s["nodes"]  # a fresh node every time (plan review B1)
        s["nodes"][name], s["cur"] = [], name
        return {"success": True}

    def _doc(self, sid):
        s = self.sessions[sid]
        return s["nodes"][s["cur"]]

    async def verify_chunk(self, sid, chunk, timeout=None, *, lease_id=None):
        if chunk in self.die:
            return {"success": False, "error_kind": "prover_died", "commands": []}
        if chunk in self.fail:
            return {"success": False, "commands": [{"status": "failed"}]}
        self._doc(sid).append(chunk)
        return {"success": True, "commands": [{"status": "ok"}], "execution_time": 0.1}

    async def save_checkpoint(self, sid, *, lease_id=None):
        cp = len(self.sessions[sid]["cps"])
        self.sessions[sid]["cps"][cp] = len(self._doc(sid))
        return {"checkpoint_id": cp, "timestamp": 0.0}

    async def restore_checkpoint(self, sid, cp, *, lease_id=None):
        del self._doc(sid)[self.sessions[sid]["cps"][cp] :]
        return {"success": True, "checkpoint_id": cp, "message": "ok"}

    async def rollback(self, sid, *, lease_id=None):
        self._doc(sid).pop()
        return {"success": True, "output": ""}

    async def get_source(self, sid, *, lease_id=None):
        return {"source": "".join(self._doc(sid))}

    async def close_session(self, sid, lease_id=None):
        self.closed.append(sid)
        return {}


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
                    {"index": i, "proved": True, "n_commands": i + 1, "wall_s": 0.5}
                    for i in range(len(cf.blocks))
                ],
            }
            for cf in files
        ],
    }
    return files, census


async def _no_sleep(_s):
    return None


def test_states_are_stratified_seeded_and_have_a_proved_continuation(tmp_path):
    files, census = _setup(tmp_path)
    assert len(files[0].blocks) == N_BLOCKS
    census["files"][0]["blocks"][4][
        "proved"
    ] = False  # Ta's state 3 has no continuation
    picks = run_t3.sample_states(files, census, 9, rng_seed=7)
    assert picks == run_t3.sample_states(files, census, 9, rng_seed=7)
    assert len(picks) == len(set(picks)) == 9
    assert ("Ta", 3) not in picks and all(i < N_BLOCKS - 1 for _s, i in picks)
    strata = [run_t3.stratum(i, N_BLOCKS) for _s, i in picks]
    assert {s: strata.count(s) for s in set(strata)} == {
        "early": 3,
        "middle": 3,
        "late": 3,
    }
    assert run_t3.stratum(0, 9) == "early" and run_t3.stratum(8, 9) == "late"


def test_a_t3_cell_on_a_fake_server(tmp_path):
    files, census = _setup(tmp_path)
    srv = Fake()
    body = asyncio.run(
        run_t3.run_cell(
            srv,
            {"states": 6},
            files,
            census,
            heap="HOL",
            seed=1,
            out_dir=tmp_path / "c",
        )
    )
    assert body["outcome"] == "ok" and body["unclosed_sessions"] == []
    states = [
        json.loads(x)
        for x in (tmp_path / "c" / "states.jsonl").read_text().splitlines()
    ]
    assert len(states) == 6 and len(srv.closed) == 6  # a session created per state
    for s in states:
        i = s["index"]
        assert s["fresh"]["blocks"] == s["warm"]["blocks"] == i + 1
        assert s["restore_ok"] and s["source_after_restore"] and s["identical"]
        assert s["rollback_ok"] and s["source_after_rollback"]
        assert s["after_rollback"]["proved"]  # the control verify after the rollback
        assert s["distance"] == i + 2  # the census count of block i + 1
        assert s["prefix_commands"] == sum(range(1, i + 2))
        # the first node ends at the state plus the control verify; the new one at the state
        nodes = srv.sessions[s["session_id"]]["nodes"]
        assert [len(d) for d in nodes.values()] == [i + 2, i + 1]
    summary = body["summary"]
    assert summary["complete"] == 6 and summary["integrity"]["identical"] == 6
    assert body["census_replay_estimate_s"] == sum(
        2 * 0.5 * (i + 1) + 4 * 0.5 for _s, i in body["picks"]
    )


def test_a_failed_continuation_skips_restore_and_a_dead_prover_stops_the_state(
    tmp_path,
):
    files, census = _setup(tmp_path, names=("Ta",))
    cf = files[0]
    srv = Fake(fail={cf.blocks[3]}, die={cf.blocks[6]})
    kw = {"heap": "HOL", "registry": {}, "on_change": lambda _r: None}
    rec = asyncio.run(run_t3.run_state(srv, cf, 2, census["files"][0], **kw))
    assert (
        rec.cont["success"] is False
        and rec.restore_s is None
        and rec.rollback_s is None
    )
    assert rec.warm["blocks"] == 3 and rec.stopped is None  # (e) still runs
    rec = asyncio.run(run_t3.run_state(srv, cf, 7, census["files"][0], **kw))
    assert rec.stopped == "prover_died" and rec.save_s is None and rec.warm is None
    assert rec.fresh["blocks"] == 7  # died at block 6
    assert srv.closed == ["s1", "s2"]
    s = metrics_t3.summarize_t3([asdict(rec)])
    assert s["complete"] == 0 and s["stopped"] == [("Ta", 7, "prover_died")]


def test_a_refused_create_is_retried(tmp_path, monkeypatch):
    monkeypatch.setattr(run_t3.asyncio, "sleep", _no_sleep)  # the Retry-After waits
    files, census = _setup(tmp_path, names=("Ta",))
    srv = Fake(refuse_creates=2)
    rec = asyncio.run(
        run_t3.run_state(
            srv,
            files[0],
            1,
            census["files"][0],
            heap="HOL",
            registry={},
            on_change=lambda _r: None,
        )
    )
    assert rec.create_retries == 2 and rec.create_wait_s == 2.0 and rec.error is None
    assert rec.identical


def _state(stratum, prefix, restore, warm, fresh_replay, create=4.0, distance=1, **kw):
    s = {
        "stem": "A",
        "index": prefix,
        "stratum": stratum,
        "prefix_commands": prefix,
        "distance": distance,
        "census_prefix_proved": 1,
        "create_s": create,
        "fresh": {"enter_s": 0.5, "replay_s": fresh_replay, "blocks": 1, "proved": 1},
        "warm": {"enter_s": 0.5, "replay_s": warm, "blocks": 1, "proved": 1},
        "save_s": 0.01,
        "cont": {"wall_s": 0.3, "success": True},
        "again": {"wall_s": 0.39, "success": True},  # 0.09 s extra after the restore
        "restore_s": restore,
        "restore_ok": True,
        "source_after_restore": True,
        "identical": True,
        "rollback_s": 0.1,
        "rollback_ok": True,
        "source_after_rollback": True,
        "after_rollback": {"wall_s": 0.3, "success": True},  # none after the rollback
        "stopped": None,
        "error": None,
    }
    s.update(kw)
    return s


def test_the_t3_summary():
    states = [
        _state("early", 10, 0.01, 1.5, 1.5, distance=1),
        _state(
            "early",
            20,
            0.02,
            2.5,
            2.5,
            distance=5,
            again={"wall_s": 0.38, "success": True},
        ),
        _state("late", 90, 0.01, 9.5, 9.5, distance=40),
        _state("late", 95, 0.01, 9.5, 9.5, distance=12, error="boom"),
    ]
    s = metrics_t3.summarize_t3(states)
    assert s["states"] == 4 and s["complete"] == 3
    early = next(r for r in s["by_stratum"] if r["stratum"] == "early")
    assert early["warm_p50_s"] == 2.5  # (2.0 + 3.0) / 2: enter + replay
    assert early["fresh_p50_s"] == 6.5  # + the create
    # the branch cost is the restore plus the extra time of the next verify:
    # 0.01 + 0.09 and 0.02 + 0.08, so (e) / branch is 2.0 / 0.1 and 3.0 / 0.1
    assert early["branch_p50_s"] == 0.1 and early["warm_over_branch_p50"] == 25.0
    assert [d["distance"] for d in s["restore_by_distance"]] == ["1", "2-9", "10+"]
    ops = s["ops"]
    assert ops["extra_after_restore"]["p50_s"] == 0.09
    assert ops["extra_after_rollback"]["p50_s"] == 0.0 and ops["branch"]["p50_s"] == 0.1
    # a verify faster than the first never makes the branch cheaper than the restore
    faster = _state("early", 1, 0.01, 1, 1, again={"wall_s": 0.1, "success": True})
    assert metrics_t3.branch_s(faster) == 0.01
    assert s["integrity"]["of"] == 4 and s["errors"] == [("A", 95, "boom")]


def test_the_t3_snippet_per_repeat(tmp_path):
    root = tmp_path / "results"
    (root / "census").mkdir(parents=True)
    (root / "census" / "census.json").write_text(
        json.dumps({"census_id": "c", "files": [], "totals": {}})
    )
    for rep in (0, 1):
        d = root / "t3" / f"rep{rep}"
        d.mkdir(parents=True)
        rows = [
            _state("early", 10, 0.01, 1.5, 1.5),
            _state("middle", 50, 0.01, 5.5, 5.5),
        ]
        (d / "states.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n")
        (d / "manifest.json").write_text(
            json.dumps(
                {"experiment": "t3", "rep": rep, "outcome": "ok", "files": ["A"]}
            )
        )
    assert analyze.main(["--results", str(root)]) == 0
    text = (root / "REPORT_SNIPPET.md").read_text()
    assert "## T3 branch cost, rep 0" in text and "## T3 branch cost, rep 1" in text
    assert "| restore checkpoint | 2 | 10 ms | 10 ms |" in text
    assert "| early | 1 | 10 |" in text and "1 early, 1 middle" in text
    assert (root / "tables" / "t3_branch.csv").exists()
    lines = (root / "tables" / "t3_states.csv").read_text().splitlines()
    assert lines[0].startswith("rep,stem,index") and len(lines) == 1 + 4
