"""Training metrics stage 7a: T6b step-level paths — the step split from the
census spans, the problem filter, a path on a fake server (statement, per
step a checkpoint, 2 seeded alternatives, a kept one restored, the corpus
command), a proved path left in the document (no advance), a failed path
undone and advanced, the text integrity check, a dead prover, and the
metrics over paths."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(
    0, str(Path(__file__).resolve().parents[1] / "evaluation" / "training_metrics")
)

from trainmetrics import corpus, metrics_t6, run_t6, t6b  # noqa: E402

KEYWORDS = ("lemma", "proof", "show", "by", "qed", "text")


def _block(name, k):
    return (
        f'lemma {name}_{k}: "x{k} = x{k}"\n'
        "proof -\n"
        "  show ?thesis\n"
        f"    by (rule refl_{k})\n"
        "qed"
    )


def _thy(name, n=3):
    return (
        f"theory {name}\n  imports Main\nbegin\n\n"
        + "\n\n".join(_block(name, k) for k in range(n))
        + "\n\nend\n"
    )


def _spans(block):
    out, pos = [], 0
    for line in block.splitlines(keepends=True):
        word = line.strip().split(" ", 1)[0]
        if word in KEYWORDS:
            out.append({"kind": word, "start": pos + len(line) - len(line.lstrip())})
        pos += len(line)
    return out


class Fake:
    """A document per session. The statement opens a proof; a corpus piece
    keeps it open, ``qed`` proves; ``apply -`` and ``by simp`` are kept with
    an open proof, other alternatives fail. ``fail`` pieces fail, ``die``
    texts report a dead prover."""

    def __init__(self, fail=(), die=()):
        self.fail, self.die = set(fail), set(die)
        self.sessions = {}
        self.closed = []
        self.n = 0

    async def acquire_session(self, **kw):
        self.n += 1
        self.sessions[f"s{self.n}"] = {"doc": [], "cps": {}}
        return {"session_id": f"s{self.n}", "lease_id": "l"}

    async def enter_theory(self, sid, name, *, imports=None, lease_id=None):
        return {"success": True}

    async def verify_chunk(self, sid, chunk, timeout=None, *, lease_id=None):
        doc, text = self.sessions[sid]["doc"], chunk.strip()
        ok = {"success": True, "commands": [{"status": "ok"}], "execution_time": 0.01}
        if chunk in self.die:
            return {"success": False, "error_kind": "prover_died", "commands": []}
        if chunk in self.fail or text in ("apply auto", "apply simp", "by auto"):
            return {"success": False, "commands": [{"status": "failed"}]}
        doc.append(chunk)
        if text == "qed" or text.startswith("lemma") and text.endswith("qed"):
            return ok
        return {**ok, "proof_open": True}

    async def save_checkpoint(self, sid, *, lease_id=None):
        cps = self.sessions[sid]["cps"]
        cps[len(cps)] = len(self.sessions[sid]["doc"])
        return {"checkpoint_id": len(cps) - 1}

    async def restore_checkpoint(self, sid, cp, *, lease_id=None):
        del self.sessions[sid]["doc"][self.sessions[sid]["cps"][cp] :]
        return {"success": True}

    async def rollback(self, sid, *, lease_id=None):
        self.sessions[sid]["doc"].pop()
        return {"success": True}

    async def get_source(self, sid, *, lease_id=None):
        return {"source": "".join(c + "\n" for c in self.sessions[sid]["doc"]).strip()}

    async def close_session(self, sid, lease_id=None):
        self.closed.append(sid)
        return {}

    async def memory_status(self):
        return {"state": "ok", "memory": {"used_mb": 1.0}, "builds_in_flight": {}}


def _setup(tmp_path, names=("Ta",)):
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
                    {
                        "index": i,
                        "proved": True,
                        "n_commands": 5,
                        "wall_s": 0.1,
                        "spans": _spans(b),
                        "spans_error": None,
                    }
                    for i, b in enumerate(cf.blocks)
                ],
            }
            for cf in files
        ],
    }
    return files, census


T6 = {
    "files_per_worker": 1,
    "tier_poll_s": 0.01,
    "integrity_fraction": 0.25,
    "t6b": {
        "problems_per_file": 2,
        "min_steps": 2,
        "max_steps": 40,
        "alternatives_per_step": 2,
        "alternative_budget_s": 10,
    },
}


def _cell(tmp_path, srv, files, census, workers=1, name="c"):
    body = asyncio.run(
        run_t6.run_cell(
            srv,
            T6,
            files,
            census,
            "t6b",
            workers,
            heap="HOL",
            seed=5,
            out_dir=tmp_path / name,
        )
    )
    events = [
        json.loads(x)
        for x in (tmp_path / name / "events.jsonl").read_text().splitlines()
    ]
    return body, events


def test_steps_from_the_census_spans_and_the_problem_filter(tmp_path):
    files, census = _setup(tmp_path)
    block = files[0].blocks[0]
    stmt, steps = t6b.steps_of(block, _spans(block))
    assert stmt.startswith("lemma") and [k for k, _ in steps] == [
        "proof",
        "show",
        "by",
        "qed",
    ]
    assert stmt + "".join(t for _, t in steps) == block
    doc = block + "\n\ntext \\<open>a note\\<close>"
    stmt2, steps2 = t6b.steps_of(doc, _spans(doc))
    assert len(steps2) == 4 and steps2[-1][1].endswith(
        "\\<close>"
    )  # merged into qed's piece
    assert (
        t6b.steps_of(block, None) is None
        and t6b.steps_of(block, _spans(block)[:1]) is None
    )
    row = census["files"][0]
    assert t6b.problems_t6b(files[0], row, 9, 2, 40) == [0, 1, 2]
    assert t6b.problems_t6b(files[0], row, 9, 5, 40) == []  # 4 steps: too short
    row["blocks"][1]["spans_error"] = "gap"
    assert t6b.problems_t6b(files[0], row, 9, 2, 40) == [0, 2]


def test_a_t6b_cell_proves_its_paths_and_keeps_them(tmp_path):
    files, census = _setup(tmp_path)
    srv = Fake()
    body, ev = _cell(tmp_path, srv, files, census)
    assert body["outcome"] == "ok" and body["unclosed_sessions"] == []
    paths = [e for e in ev if e["kind"] == "path"]
    assert [(e["index"], e["outcome"], e["steps"]) for e in paths] == [
        (0, "proved", 4),
        (1, "proved", 4),
    ]
    alts = [e for e in ev if e["kind"] == "alternative"]
    assert len(alts) == 2 * 4 * 2  # 2 paths x 4 steps x 2 alternatives
    kept = [e for e in alts if e["outcome"] in ("proved", "kept_unproved")]
    restores = [e for e in ev if e["kind"] == "restore"]
    assert len(restores) == len(kept) > 0  # every kept alternative restored
    # a proved path stays in the document: its block is not advanced again
    assert not [e for e in ev if e["kind"] == "advance" and e["index"] in (0, 1)]
    checks = [e for e in ev if e["kind"] == "integrity" and e["at"] == "path_end"]
    assert len(checks) == 2 and all(e["ok"] for e in checks)  # W = 1: every path
    s = body["summary"]
    assert s["rollouts"] == 2 and s["proved_rollouts"] == 2 and s["problems"] == 2
    assert s["steps_per_h"] > 0 and s["alternatives"]["n"] == 16
    assert set(s["by_candidate"]) <= set(t6b.ALTERNATIVES)
    assert "path" not in "".join(s["shares"])  # the summary is not a call
    assert srv.sessions["s1"]["doc"] == [
        p
        for b in files[0].blocks[:2]
        for p in (lambda st: [st[0]] + [x for _, x in st[1]])(
            t6b.steps_of(b, _spans(b))
        )
    ]


def test_a_failed_step_undoes_the_path_and_advances(tmp_path):
    files, census = _setup(tmp_path)
    steps = t6b.steps_of(files[0].blocks[0], _spans(files[0].blocks[0]))[1]
    by_0 = steps[2][1]  # "by (rule refl_0)": block 0's only
    srv = Fake(fail={by_0})
    body, ev = _cell(tmp_path, srv, files, census)
    first = [e for e in ev if e["kind"] == "path"][0]
    assert first["outcome"] == "failed"
    undo = [e for e in ev if e["kind"] == "restore" and e.get("role") == "path"]
    assert len(undo) == 1
    adv = [e for e in ev if e["kind"] == "advance" and e["index"] == 0]
    assert len(adv) == 1 and adv[0]["proved"]  # the corpus block, whole
    checks = [e for e in ev if e["kind"] == "integrity" and e["at"] == "path_end"]
    assert all(e["ok"] for e in checks)
    assert body["summary"]["rollouts"] == 2 and body["summary"]["proved_rollouts"] == 1


def test_a_dead_prover_in_an_alternative_stops_the_file(tmp_path):
    files, census = _setup(tmp_path)
    srv = Fake(die=set(t6b.ALTERNATIVES))  # the first alternative tried dies
    body, ev = _cell(tmp_path, srv, files, census)
    died = [e for e in ev if e["kind"] == "alternative"]
    assert len(died) == 1 and died[0]["outcome"] == "prover_died"
    assert body["summary"]["stopped"] == [("Ta", 0, "prover_died")]
    assert body["summary"]["outcomes_apart"] == {"prover_died": 1}
    assert srv.closed == ["s1"]


def _e(kind, t, wall, **kw):
    return {
        "worker": 0,
        "stem": "A",
        "index": 3,
        "kind": kind,
        "t_start": t,
        "wall_s": wall,
        **kw,
    }


def test_the_metrics_over_paths():
    events = [
        _e("checkpoint", 10.0, 0.1, role="path"),
        _e("step", 10.1, 0.5, role="statement", outcome="kept_unproved"),
        _e("checkpoint", 10.6, 0.1, role="step"),
        _e("alternative", 10.7, 1.0, outcome="kept_unproved", candidate="apply -"),
        _e("restore", 11.7, 0.1),
        _e("alternative", 11.8, 10.0, outcome="timed_out", candidate="by auto"),
        _e("step", 21.8, 0.4, role="step", outcome="proved"),
        _e("path", 10.0, 12.2, outcome="proved", steps=1, summary=True),
        _e("integrity", 22.2, 0.05, at="path_end", ok=True),
        _e("close", 22.25, 0.5),
    ]
    assert metrics_t6.loop_time(events) == 22.2 - 10.0
    s = metrics_t6.summarize_t6(
        events,
        [{"stem": "A", "worker": 0, "groups_done": 1}],
        {"window_start": 0.0, "window_end": 36.0, "workers": 1, "matched_stems": ["A"]},
    )
    assert (
        s["rollouts"] == 1 and s["proved_rollouts"] == 1 and s["steps_per_h"] == 100.0
    )
    assert s["by_candidate"]["by auto"]["timeout_share"] == 1.0
    assert s["alternatives"] == {"n": 2, "kept": 1, "kept_share": 0.5}
    assert (
        s["shares"]["alternative_timed_out"] == round(10 / 36, 4)
        and "path" not in s["shares"]
    )
    assert s["matched"]["per_rollout_s"] == round(
        0.1 + 0.5 + 0.1 + 1 + 0.1 + 10 + 0.4, 4
    )
