"""Training metrics stage 6a: the T6a cell — the D9 candidates, problems,
the group on a fake server (checkpoint, candidates in a seeded order, a kept
one restored, integrity reads), advances retried once at the maximum budget,
a dead prover and a failed restore stopping the file, and the summary (both
rates, shares, per candidate, matched files)."""

from __future__ import annotations

import asyncio
import json
import random
import sys
from pathlib import Path

import pytest

sys.path.insert(
    0, str(Path(__file__).resolve().parents[1] / "evaluation" / "training_metrics")
)

from trainmetrics import corpus, metrics_t6, run_t6, t6_core  # noqa: E402

N_BLOCKS = 6


def _thy(name):
    lemmas = "\n\n".join(
        f'lemma {name}_{k}: "x{k} = x{k}"\n  by (rule refl)' for k in range(N_BLOCKS)
    )
    return f"theory {name}\n  imports Main\nbegin\n\n{lemmas}\n\nend\n"


class Fake:
    """A document per node. A corpus block proves; ``sorry`` is kept with
    used_sorry; ``by auto`` / ``by simp`` / ``FalseE`` fail; any other text
    (the truncated statement) is kept with an open proof. ``flaky[chunk]``
    failures come before a chunk proves; ``die`` chunks report a dead prover;
    ``bad_restore`` makes every restore fail."""

    def __init__(self, originals, flaky=None, die=(), bad_restore=False):
        self.originals = set(originals)
        self.flaky = dict(flaky or {})
        self.die = set(die)
        self.bad_restore = bad_restore
        self.sessions = {}
        self.closed = []
        self.budgets = []
        self.n = 0

    async def acquire_session(self, **kw):
        self.n += 1
        sid = f"s{self.n}"
        self.sessions[sid] = {"doc": [], "cps": {}}
        return {"session_id": sid, "lease_id": "l"}

    async def enter_theory(self, sid, name, *, imports=None, lease_id=None):
        return {"success": True}

    async def verify_chunk(self, sid, chunk, timeout=None, *, lease_id=None):
        self.budgets.append((chunk, timeout))
        ok = {"commands": [{"status": "ok"}], "execution_time": 0.01}
        if chunk in self.die:
            return {"success": False, "error_kind": "prover_died", "commands": []}
        if self.flaky.get(chunk):
            self.flaky[chunk] -= 1
            return {"success": False, "commands": [{"status": "failed"}]}
        doc = self.sessions[sid]["doc"]
        text = chunk.rstrip()
        if chunk in self.originals:
            doc.append(chunk)
            return {"success": True, **ok}
        if text.endswith("sorry"):
            doc.append(chunk)
            return {"success": True, "used_sorry": True, **ok}
        if text.endswith(("by auto", "by simp", "FalseE)")):
            return {"success": False, "commands": [{"status": "failed"}]}
        doc.append(chunk)
        return {"success": True, "proof_open": True, **ok}

    async def save_checkpoint(self, sid, *, lease_id=None):
        cps = self.sessions[sid]["cps"]
        cps[len(cps)] = len(self.sessions[sid]["doc"])
        return {"checkpoint_id": len(cps) - 1}

    async def restore_checkpoint(self, sid, cp, *, lease_id=None):
        if self.bad_restore:
            return {"success": False, "message": "unknown"}
        del self.sessions[sid]["doc"][self.sessions[sid]["cps"][cp] :]
        return {"success": True}

    async def rollback(self, sid, *, lease_id=None):
        self.sessions[sid]["doc"].pop()
        return {"success": True}

    async def get_source(self, sid, *, lease_id=None):
        return {"source": "".join(self.sessions[sid]["doc"])}

    async def close_session(self, sid, lease_id=None):
        self.closed.append(sid)
        return {}

    async def memory_status(self):
        return {"state": "ok", "memory": {"used_mb": 1.0}, "builds_in_flight": {}}


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
    }
    return files, census, [b for cf in files for b in cf.blocks]


T6 = {
    "files_per_worker": 1,
    "tier_poll_s": 0.01,
    "integrity_fraction": 0.25,
    "t6a": {"problems_per_file": 2, "candidate_budget_s": 20},
}


def test_the_d9_candidates_and_the_problems(tmp_path):
    files, census, _ = _setup(tmp_path, names=("Ta",))
    cf = files[0]
    cands = t6_core.candidates_t6a(cf.blocks[1])
    kinds = [k for k, _ in cands]
    assert kinds == [
        "corpus",
        "by_auto",
        "by_auto",
        "by_simp",
        "by_simp",
        "sorry",
        "truncated",
        "false_e",
    ]
    texts = dict(cands)
    assert texts["sorry"].endswith("sorry") and texts["truncated"].rstrip().endswith(
        '"x1 = x1"'
    )
    assert texts["false_e"].endswith("by (rule FalseE)")
    census["files"][0]["blocks"][0]["proved"] = False
    assert t6_core.problems_of(cf, census["files"][0], 3) == [1, 2, 3]


def _cell(tmp_path, srv, files, census, workers, name="c"):
    return asyncio.run(
        run_t6.run_cell(
            srv,
            T6,
            files,
            census,
            "t6a",
            workers,
            heap="HOL",
            seed=3,
            out_dir=tmp_path / name,
        )
    )


def _events(tmp_path, name="c"):
    return [
        json.loads(x)
        for x in (tmp_path / name / "events.jsonl").read_text().splitlines()
    ]


def test_a_t6a_cell_on_a_fake_server(tmp_path):
    files, census, originals = _setup(tmp_path)
    srv = Fake(originals)
    body = _cell(tmp_path, srv, files, census, workers=2)
    assert body["outcome"] == "ok" and body["unclosed_sessions"] == []
    ev = _events(tmp_path)
    roll = [e for e in ev if e["kind"] == "rollout"]
    assert len(roll) == 2 * 2 * 8  # 2 files x 2 problems x the 8 candidates
    by = {}
    for e in roll:
        by.setdefault(e["candidate"], set()).add(e["outcome"])
    assert by["corpus"] == {"proved"} and by["sorry"] == {"kept_unproved"}
    assert by["truncated"] == {"kept_unproved"} and by["by_auto"] == {"failed"}
    restores = [e for e in ev if e["kind"] == "restore"]
    assert len(restores) == 2 * 2 * 3  # the corpus proof, sorry, truncated
    # the problems' corpus blocks advance after their group; block 0 and 1 are the problems
    adv = [e for e in ev if e["kind"] == "advance"]
    assert (
        all(e["proved"] and e["phase"] == "advance" for e in adv) and len(adv) == 2 * 2
    )
    # the seeded order differs between problems but is fixed by the seed
    orders = [
        [e["candidate"] for e in roll if e["stem"] == s and e["index"] == i]
        for s in ("Ta", "Tb")
        for i in (0, 1)
    ]
    assert len({tuple(o) for o in orders}) > 1
    rng = random.Random("3:Ta:0")  # f"{seed}:{stem}:{index}"
    expect = t6_core.candidates_t6a(files[0].blocks[0])
    rng.shuffle(expect)
    assert orders[0] == [k for k, _ in expect]
    # each session ends with the problems' corpus blocks only: every candidate undone
    for sid in srv.closed:
        assert srv.sessions[sid]["doc"] == [
            b for b in originals if b in srv.sessions[sid]["doc"]
        ]
        assert len(srv.sessions[sid]["doc"]) == 2
    s = body["summary"]
    assert s["rollouts"] == 32 and s["proved_rollouts"] == 4 and s["problems"] == 4
    assert s["restores_per_rollout"] == round(12 / 32, 3)
    assert (
        s["rollouts_per_h"] > 0
    )  # the fake's calls take ~0 s: no loop rate to test here
    assert {w: v["rollouts"] for w, v in s["workers"].items()} == {0: 16, 1: 16}
    assert s["by_candidate"]["false_e"]["outcomes"] == {"failed": 4}
    assert body["server_executor_threads"] >= 5 and body["integrity_p"] == 0.25


def test_every_group_is_checked_at_w1(tmp_path):
    files, census, originals = _setup(tmp_path, names=("Ta",))
    body = _cell(tmp_path, Fake(originals), files, census, workers=1)
    ev = _events(tmp_path)
    reads = [e for e in ev if e["kind"] == "integrity"]
    assert [e["at"] for e in reads] == ["checkpoint", "group_end"] * 2
    assert all(e["ok"] for e in reads if e["at"] == "group_end")
    # never a read between a restore and the next loop call
    for k, e in enumerate(ev[:-1]):
        if e["kind"] == "restore":
            assert ev[k + 1]["kind"] in ("rollout", "integrity")
            if ev[k + 1]["kind"] == "integrity":
                assert ev[k + 1]["at"] == "group_end"
    integ = body["summary"]["integrity"]
    assert integ["checked"] == integ["ok"] == 2 and integ["reads"]["n"] == 4
    assert integ["groups"] == 2 and integ["groups_unchecked"] == 0
    assert body["matched_stems"] == ["Ta"]
    # review f0d85df S2: the session's close is an event (outside the loop)
    assert [e["kind"] for e in ev][-1] == "close"


def test_a_leftover_after_the_group_fails_the_check(tmp_path):
    files, census, originals = _setup(tmp_path, names=("Ta",))
    srv = Fake(originals)
    real = srv.restore_checkpoint

    async def sloppy(
        sid, cp, *, lease_id=None
    ):  # restores, then leaves one line behind
        out = await real(sid, cp, lease_id=lease_id)
        srv.sessions[sid]["doc"].append("junk")
        return out

    srv.restore_checkpoint = sloppy
    body = _cell(tmp_path, srv, files, census, workers=1)
    s = body["summary"]
    # review f0d85df S1: the file stops, the group's rollouts are counted apart
    assert s["integrity"]["ok"] == 0 and s["integrity"]["failed_groups"] == [
        (0, "Ta", 0)
    ]
    assert s["stopped"] == [("Ta", 0, "integrity_failed")]
    assert s["rollouts"] == 0 and s["integrity"]["rollouts_in_failed_groups"] == 8


def test_an_advance_is_retried_once_at_the_maximum_budget(tmp_path):
    files, census, originals = _setup(tmp_path, names=("Ta",))
    cf = files[0]
    srv = Fake(
        originals, flaky={cf.blocks[0]: 2}
    )  # the corpus candidate and the advance fail once each
    body = _cell(tmp_path, srv, files, census, workers=1)
    adv = [e for e in _events(tmp_path) if e["kind"] == "advance"]
    assert [e.get("retried", False) for e in adv[:2]] == [False, True]
    assert adv[1]["proved"] and adv[1]["budget_s"] == 600.0
    assert (
        body["summary"]["advances"]["retried"] == 1 and not body["summary"]["stopped"]
    )
    srv = Fake(
        originals, flaky={cf.blocks[0]: 3}
    )  # the retry fails too: the file stops
    body = _cell(tmp_path, srv, files, census, workers=1, name="c2")
    assert body["summary"]["stopped"] == [("Ta", 0, "advance_failed")]


def test_a_dead_prover_or_a_failed_restore_stops_the_file(tmp_path):
    files, census, originals = _setup(tmp_path, names=("Ta",))
    sorry = t6_core.candidates_t6a(files[0].blocks[0])[5][1]
    srv = Fake(originals, die={sorry})
    body = _cell(tmp_path, srv, files, census, workers=1)
    assert body["summary"]["stopped"] == [("Ta", 0, "prover_died")]
    assert body["summary"]["outcomes_apart"] == {"prover_died": 1}
    assert srv.closed == ["s1"]
    body = _cell(
        tmp_path, Fake(originals, bad_restore=True), files, census, workers=1, name="c2"
    )
    assert body["summary"]["stopped"] == [("Ta", 0, "restore_failed")]


def _e(worker, kind, t, wall, **kw):
    return {
        "worker": worker,
        "stem": "A",
        "kind": kind,
        "t_start": t,
        "wall_s": wall,
        **kw,
    }


def test_the_loop_time_and_the_rates():
    events = [
        _e(0, "acquire", 0.0, 5.0),
        _e(0, "enter", 5.0, 1.0),
        _e(0, "advance", 6.0, 4.0, phase="prefix", proved=True, census_proved=True),
        _e(0, "integrity", 10.0, 0.05, at="checkpoint", ok=None),
        _e(0, "checkpoint", 10.05, 0.1),
        _e(0, "rollout", 10.15, 1.0, outcome="proved", candidate="corpus"),
        _e(0, "restore", 11.15, 0.1),
        _e(0, "rollout", 11.25, 2.0, outcome="failed", candidate="by_auto"),
        _e(0, "integrity", 13.25, 0.15, at="group_end", ok=True),
        _e(0, "advance", 13.4, 0.6, phase="advance", proved=True, census_proved=True),
        _e(0, "close", 14.0, 0.4),  # a file switch inside the span: outside the loop
        _e(0, "checkpoint", 14.4, 0.1),
        _e(0, "rollout", 14.5, 1.5, outcome="timed_out", candidate="by_simp"),
        _e(0, "rollout", 16.0, 0.0, outcome="not_applicable", candidate="truncated"),
        _e(0, "rollout", 16.0, 0.5, outcome="memory", candidate="sorry"),
    ]
    # from the first checkpoint (10.05) to the last rollout's end (16.5), minus
    # the group-end read (0.15), the advance (0.6) and the close (0.4) inside
    assert metrics_t6.loop_time(events) == pytest.approx(6.45 - 1.15)
    s = metrics_t6.summarize_t6(
        events,
        [{"stem": "A", "worker": 0, "groups_done": 2}],
        {"window_start": 0.0, "window_end": 18.0, "workers": 1, "matched_stems": ["A"]},
    )
    assert s["rollouts"] == 3 and s["outcomes_apart"] == {"memory": 1}
    assert s["rollouts_per_h"] == round(3 / (18 / 3600), 1)
    assert s["loop_rollouts_per_h"] == round(3 / 5.3 * 3600, 1)
    sh = s["shares"]
    assert sh["prefix"] == round(4 / 18, 4) and sh["verify_failed"] == round(2 / 18, 4)
    assert sh["integrity"] == round(0.2 / 18, 4)
    assert s["integrity"]["checked"] == 1 and s["integrity"]["reads"]["n"] == 2
    assert s["matched"]["rollouts"] == 3
    assert s["matched"]["per_rollout_s"] == round(
        (0.1 + 1 + 0.1 + 2 + 0.1 + 1.5 + 0.5) / 3, 4
    )


def test_the_matrix_runs_t6a_cells_from_the_t6_section(tmp_path):
    from trainmetrics import run_matrix  # pylint: disable=import-outside-toplevel

    cfg = {"workers": [1, 2, 4], "repeats": 3, "t6": {"workers": [8, 1], "repeats": 2}}
    assert list(run_matrix.cells(cfg, experiment="t6a")) == [
        (1, 0),
        (1, 1),
        (8, 0),
        (8, 1),
    ]
    assert (
        run_matrix.cell_path(tmp_path, 8, 1, "t6a") == tmp_path / "t6a" / "w8" / "rep1"
    )
    ns = run_matrix.argparse.Namespace(
        experiment="t6a",
        config="c",
        census="x.json",
        out="o",
        local_smoke=False,
        ips_external=True,
        url=None,
        quota=1,
    )
    cmd = run_matrix.child_cmd(ns, 8, 1)
    assert cmd[3:6] == ["trainmetrics.run_t6", "--style", "t6a"]
    assert cmd[-2:] == ["--files-per-worker", "1"] and "--ips-external" in cmd


def _cells_on_disk(tmp_path):
    files, census, originals = _setup(tmp_path, names=("Ta", "Tb"))
    root = tmp_path / "results"
    for w in (1, 2):
        out = run_t6.cell_dir(root, "t6a", w, 0)
        body = asyncio.run(
            run_t6.run_cell(
                Fake(originals),
                T6,
                files,
                census,
                "t6a",
                w,
                heap="HOL",
                seed=3,
                out_dir=out,
            )
        )
        body["rep"] = 0
        (out / "manifest.json").write_text(json.dumps(body, default=str))
    return root


def test_the_t6a_analysis(tmp_path):
    from trainmetrics import analyze_t6  # pylint: disable=import-outside-toplevel

    root = _cells_on_disk(tmp_path)
    tables = root / "tables"
    tables.mkdir()
    text, n, _plots = analyze_t6.write_t6(root, tables)
    assert n == 2 and "## T6a whole-proof rollouts" in text
    rows = [analyze_t6.cell_row(c) for c in analyze_t6.load_t6_cells(root, "t6a")]
    ser = analyze_t6.series(rows)
    assert [s["workers"] for s in ser] == [1, 2] and ser[0]["e2e_speedup"] == 1.0
    assert "| 1 | 1 |" in text and "| 2 | 1 |" in text
    for name in ("t6a_cells.csv", "t6a_series.csv", "t6a_candidates.csv"):
        assert (tables / name).exists()


def test_the_calibration_and_the_timeout_for_the_slowest_worker(tmp_path):
    """Review 5422bf0 S1: the timeout comes from every worker of the matrix
    (census times up to the last problem + the calibrated candidates), not
    from the small calibration file."""
    from trainmetrics import calibrate_t6  # pylint: disable=import-outside-toplevel

    root = _cells_on_disk(tmp_path)
    cal = calibrate_t6.calibration(run_t6.cell_dir(root, "t6a", 1, 0))
    assert cal["rollouts"] == 16 and cal["timeout_share"] == 0.0
    assert cal["other_candidate_mean_s"] is not None and cal["setup_s"] >= 0
    (tmp_path / "x").mkdir()
    files, census, _ = _setup(tmp_path / "x", names=("Ta", "Tb"))
    census["files"][1]["blocks"][3]["wall_s"] = 3000.0  # Tb's 4th block is slow
    cal = {"other_candidate_mean_s": 1.0, "restore_p50_s": 0.0, "setup_s": 10.0}
    t6 = {
        "workers": [1, 2],
        "repeats": 1,
        "files_per_worker": 1,
        "t6a": {"problems_per_file": 4},
    }
    est = calibrate_t6.estimate(cal, census, files, t6, seed=3, contention=3.0)
    w = est["largest_worker"]
    assert w["stems"] == ["Tb"]
    # setup 10 + census 0.1 x 3 + 3000 up to block 3, + per problem: its census time + 7 x 1.0
    assert w["estimate_s"] == round(10 + 0.3 + 3000 + (0.1 * 3 + 3000) + 4 * 7, 1)
    assert est["suggested_cell_timeout_s"] == 18116  # ceil(6038.6 x 3), above the floor
    small = calibrate_t6.estimate(
        cal,
        {**census, "files": census["files"][:1]},
        files[:1],
        t6,
        seed=3,
        contention=3.0,
    )
    assert small["suggested_cell_timeout_s"] == 7200  # the floor
