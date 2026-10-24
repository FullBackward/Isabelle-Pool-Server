"""Training metrics stage 3c-1: T5's contention cell — seeds per worker, the
overlap of other workers' calls with a loop, the memory tier poll, the
prover matched by its session row (review 1676d29 S1), a whole cell on a
fake server."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(
    0, str(Path(__file__).resolve().parents[1] / "evaluation" / "training_metrics")
)

from trainmetrics import contention, corpus, metrics_t5, run_t5w8, t5_core  # noqa: E402

THY = """theory {name}
  imports Main
begin

lemma {name}_a: "(a::nat) + b = b + a"
  by simp

lemma {name}_b: "rev (rev xs) = xs"
  by simp

lemma {name}_c: "length (xs @ ys) = length ys + length xs"
  by simp

lemma {name}_d: "(x::nat) + 0 = x"
  by simp

end
"""


@pytest.fixture(autouse=True)
def _no_settle_pause(monkeypatch):
    monkeypatch.setattr(t5_core, "SETTLE_S", 0.0)


class Fake:
    """Seeds loop (timed out), corpus blocks prove; memory rows per session."""

    def __init__(self):
        self.n = 0
        self.docs = {}
        self.polls = 0

    async def acquire_session(self, **kw):
        self.n += 1
        sid = f"s{self.n}"
        self.docs[sid] = []
        return {"session_id": sid, "lease_id": f"l{self.n}", "reused": False}

    async def enter_theory(self, sid, name, *, imports=None, lease_id=None):
        return {"success": True}

    async def verify_chunk(self, sid, chunk, timeout=None, *, lease_id=None):
        await asyncio.sleep(0)
        if "t5_seed_" in chunk:
            return {
                "success": False,
                "timed_out": True,
                "stuck_line": 2,
                "commands": [{"status": "ok"}, {"status": "running"}],
            }
        self.docs[sid].append(chunk)
        return {
            "success": True,
            "timed_out": False,
            "execution_time": 0.001,
            "commands": [{"status": "ok"}, {"status": "ok"}],
        }

    async def get_source(self, sid, lease_id=None):
        return {"source": "\n".join(self.docs[sid])}

    async def close_session(self, sid, lease_id=None):
        return {}

    async def memory_status(self):
        self.polls += 1
        return {
            "state": "ok",
            "memory": {
                "used_mb": 900.0 + self.polls,
                "limit_mb": 4000.0,
                "pressure_pct": 22.5,
            },
            "sessions": [],
        }


def _corpus(tmp_path, names=("Fa", "Fb")):
    files = []
    for n in names:
        p = tmp_path / f"{n}.thy"
        p.write_text(THY.format(name=n))
        files.append(corpus.load_file(p))
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
            for cf in files
        ],
    }
    return files, census


W8 = {
    "workers": 2,
    "files_per_worker": 1,
    "seed_fraction": 0.5,
    "max_seeds_per_worker": 2,
    "budget_s": 5,
    "tier_poll_s": 0.01,
}


def test_a_worker_gets_a_capped_share_of_its_blocks_as_seeds(tmp_path):
    files, census = _corpus(tmp_path)
    sd = run_t5w8.worker_seeds(files[:1], census, W8, worker=3, seed=1)
    assert len(sd) == 2 and {s.family for s in sd} == {"subst", "metis"}
    assert all(s.seed_id.startswith("w3:") for s in sd)
    few = dict(W8, seed_fraction=0.01)  # ceil(0.01 * 4 blocks) = 1
    assert len(run_t5w8.worker_seeds(files[:1], census, few, worker=0, seed=1)) == 1


def test_a_contention_cell_on_a_fake_server(tmp_path):
    files, census = _corpus(tmp_path)
    out = tmp_path / "cell"
    srv = Fake()
    body = asyncio.run(
        run_t5w8.run_cell(
            srv, {"w8": W8}, files, census, heap="HOL", seed=1, out_dir=out
        )
    )
    assert body["outcome"] == "ok" and body["workers"] == 2 and body["seeds"] == 4
    assert body["server_executor_threads"] >= 5 and body["unclosed_sessions"] == []
    recs = [json.loads(x) for x in (out / "positions.jsonl").read_text().splitlines()]
    assert sorted(r["worker"] for r in recs) == [0, 1]
    for r in recs:  # the whole file replayed, every corpus block recorded
        assert len(r["blocks"]) == 4 and all(b["proved"] for b in r["blocks"])
    tiers = [json.loads(x) for x in (out / "tiers.jsonl").read_text().splitlines()]
    assert tiers and tiers[-1]["state"] == "ok"
    s = body["summary"]
    assert s["tiers"]["states"]["ok"] == len(tiers) and s["tiers"]["used_max_mb"] > 900
    assert s["contention"]["loops"] == 4
    assert sum(r["n"] for r in s["contention"]["by_state"]) == 8


def test_blocks_are_grouped_by_what_the_other_workers_were_doing():
    """Review 73f9cd4 S1: a block that misses a loop is no quiet baseline; the
    comparison is a loop against replay at the same number of others working."""

    def att(t, wall, outcome="timed_out"):
        return {"outcome": outcome, "t_start": t, "wall_s": wall}

    def blk(i, t, wall):
        return {"index": i, "t_start": t, "wall_s": wall, "execution_time": wall - 0.1}

    files = [
        {  # worker 0: setup 0-2, replay 2-10, a loop 10-20, replay 20-30, done at 31
            "stem": "A",
            "worker": 0,
            "t_start": 0.0,
            "acquire_s": 1.5,
            "enter_s": 0.5,
            "t_end": 31.0,
            "positions": [{"attempts": [att(10.0, 10.0)]}],
            "blocks": [blk(0, 2.0, 8.0), blk(1, 20.0, 10.0)],
        },
        {  # worker 1: one block per state of worker 0
            "stem": "B",
            "worker": 1,
            "t_start": 0.0,
            "acquire_s": 0.2,
            "enter_s": 0.1,
            "t_end": 41.0,
            "positions": [],
            "blocks": [
                blk(0, 0.5, 1.0),
                blk(1, 5.0, 1.0),
                blk(2, 14.0, 2.0),
                blk(3, 25.0, 1.0),
                blk(4, 35.0, 1.0),
            ],
        },
    ]
    walls = {
        ("A", 0): 8.0,
        ("A", 1): 10.0,
        ("B", 0): 1.0,
        ("B", 1): 1.0,
        ("B", 2): 1.0,
        ("B", 3): 1.0,
        ("B", 4): 1.0,
    }
    t = contention.state_table(files, walls)
    by = {tuple(k for k in contention.STATES if r[k]): r for r in t["by_state"]}
    # worker 1's blocks: during 0's setup, replay, loop, replay, after it finished
    assert (
        by[("setup",)]["n"] == 1 and by[("loop",)]["n"] == 1 and by[("done",)]["n"] == 1
    )
    assert by[("loop",)]["ratio_p50"] == 2.0 and by[("replay",)]["ratio_p50"] == 1.0
    # B is in setup while A's first block starts (midpoint 6.0: replaying)
    assert t["loops"] == 1
    assert t["loop_vs_replay"] == {"blocks": 1, "slowdown": 2.0, "slowdown_p95": 2.0}
    iv, end = contention.worker_intervals(files)[0]
    assert contention.state_at(iv, end, 15.0) == "loop"
    assert contention.state_at(iv, end, 1.0) == "setup"
    assert contention.state_at(iv, end, 30.5) == "idle"
    assert contention.state_at(iv, end, 40.0) == "done"


def test_the_tier_poll_and_its_summary(tmp_path):
    srv = Fake()
    path = tmp_path / "tiers.jsonl"

    async def go():
        stop = asyncio.Event()
        task = asyncio.ensure_future(contention.poll_tiers(srv, path, stop, 0.01))
        await asyncio.sleep(0.05)
        stop.set()
        await task

    asyncio.run(go())
    rows = metrics_t5.load_samples(path)
    assert len(rows) >= 2
    s = contention.tier_summary(
        rows + [{"t": 1, "error": "x"}, {"t": 2, "state": "warning"}]
    )
    assert (
        s["states"]["warning"] == 1
        and s["errors"] == 1
        and s["pressure_max_pct"] == 22.5
    )


def _sample(t, provers):
    return {
        "t": t,
        "procs": [
            {"pid": pid, "role": "poly", "rss": int(mb * 2**20), "cpu_ticks": ticks}
            for pid, (mb, ticks) in provers.items()
        ],
    }


def test_sessions_provers_are_found_by_their_lifetimes():
    """Review 1676d29 S1: at W > 1 the busiest prover is another worker's. A
    session's prover is born during its acquisition and gone at its close; its
    helper poly (born with it, a few % of its CPU) is not picked; provers are
    assigned jointly, so a session whose acquisition window holds two births
    gets the one the other session did not take."""
    samples = [
        _sample(100.0, {10: (1000, 0), 11: (30, 0)}),
        _sample(101.0, {10: (1200, 200), 11: (30, 5), 20: (2000, 0)}),
        _sample(
            102.0, {10: (1400, 400), 11: (30, 10), 20: (2100, 400)}
        ),  # 20 works harder
        _sample(
            103.0, {10: (1300, 401), 11: (30, 10), 20: (2200, 800)}
        ),  # 10 in its pause
        _sample(104.0, {20: (2300, 1200)}),  # session A closed: 10 and its helper gone
        _sample(105.0, {20: (2300, 1300)}),
    ]
    a = {
        "seed_id": "w0:subst-0",
        "family": "subst",
        "budget_s": 1.0,
        "outcome": "timed_out",
        "wall_s": 2.0,
        "t_start": 100.5,
        "t_settled": 103.5,
    }
    rec_a = {
        "stem": "A",
        "worker": 0,
        "t_start": 99.5,
        "acquire_s": 0.6,
        "t_end": 103.8,
        "positions": [{"position": 1, "attempts": [a]}],
    }
    rec_b = {
        "stem": "B",
        "worker": 1,
        "t_start": 99.5,
        "acquire_s": 2.0,
        "t_end": 105.4,
        "positions": [],
    }
    assert contention.executor_threads() >= 5
    assert metrics_t5.session_pids(samples, [rec_a, rec_b]) == [(10, None), (20, None)]
    # alone, B's acquisition window holds both births, but A's prover is idle
    # during B's life after its pause: only 20 has the CPU
    assert metrics_t5.session_pids(samples, [rec_b]) == [(20, None)]
    metrics_t5.annotate([rec_a, rec_b], samples, by_session=True)
    pm = a["prover_mem"]
    assert pm["pid"] == 10 and pm["retained_mb"] == 300.0 and pm["censored"] is None
    # two sessions acquired and closed together cannot be told apart
    twin = [
        _sample(t, {10: (1000, 100 * i), 20: (1000, 120 * i)})
        for i, t in enumerate((100.0, 101.0, 102.0, 103.0))
    ]
    rec_c = dict(rec_a, t_end=103.6)
    rec_d = dict(rec_a, stem="D", t_end=103.7)
    assert metrics_t5.session_pids(twin, [rec_c, rec_d]) == [(None, "ambiguous")] * 2
    late = dict(rec_a, t_start=200.0, t_end=210.0)
    assert metrics_t5.session_pids(samples, [late]) == [(None, "no_match")]


def test_a_late_helper_does_not_hide_its_prover():
    """Review 73f9cd4 S2: under load the acquisition took long and the helper
    poly was born 5.5 s after its prover, both inside the acquisition; it
    stays ~30 MB (with tens of CPU ticks, above the 20-tick floor), so it is
    dropped by its size, not by its birth."""
    rows = [(100.0 + i, {10: (900 + 10 * i, 300 * i)}) for i in range(6)]
    rows += [
        (105.5 + i, {10: (960, 1800 + 300 * i), 11: (30, 5 * i)}) for i in range(10)
    ]
    samples = [_sample(t, p) for t, p in rows] + [_sample(116.0, {})]
    rec = {
        "stem": "A",
        "worker": 0,
        "t_start": 99.5,
        "acquire_s": 8.0,
        "t_end": 115.8,
        "positions": [],
    }
    assert metrics_t5.session_pids(samples, [rec]) == [(10, None)]


def test_the_no_loop_baseline_is_pooled_whatever_the_order():
    """Review e0f8ac6 B1: baseline groups that share a working count (here one
    other replaying, the rest idle or done) are pooled, so the slowdown does
    not depend on which group came first."""
    one = {"ratio": 1.0, "queue_s": 0.0}
    two = {"ratio": 2.0, "queue_s": 0.0}
    mid = {"ratio": 1.5, "queue_s": 0.0}
    # keys: (loop, replay, setup, idle, done) among three others
    replay_idle = ((0, 1, 0, 2, 0), [one] * 10)
    replay_done = ((0, 1, 0, 0, 2), [two] * 10)
    loop_idle = ((1, 0, 0, 2, 0), [mid] * 10)
    for order in (
        [replay_idle, replay_done, loop_idle],
        [replay_done, replay_idle, loop_idle],
    ):
        rel = contention._relative(dict(order))  # pylint: disable=protected-access
        assert rel and all(abs(x - 1.0) < 1e-9 for x in rel)  # pooled p50 1.5 -> 1.0
