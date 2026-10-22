"""Training metrics, stage 2 (plan v1.3), part 2: sample windows with per-pid
CPU deltas, the T1 cell against a fake client, and the matrix runner's resume
and lock."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(
    0, str(Path(__file__).resolve().parents[1] / "evaluation" / "training_metrics")
)

from trainmetrics import corpus, run_matrix, run_t1, samples  # noqa: E402

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


def test_cpu_by_role_uses_per_pid_deltas():
    """Review a66fa6e S1: a pid that appears counts from its first sample, one
    that vanishes up to its last; the total of a snapshot is never differenced."""
    rows = [
        _row(0, [(1, "jvm", 100), (2, "poly", 1000)]),
        _row(1, [(1, "jvm", 150), (2, "poly", 1200), (3, "poly", 5000)]),
        _row(2, [(1, "jvm", 160), (3, "poly", 5300)]),  # pid 2 gone, pid 3 old CPU
    ]
    cpu = samples.cpu_by_role(rows)
    assert cpu["jvm"] == 0.6 and cpu["poly"] == 5.0 and cpu["total"] == 5.6


def test_window_summary_host_cgroup_memory():
    rows = [
        _row(10, [(1, "jvm", 0)], busy=0, total=0, usage=0, pss=100),
        _row(11, [(1, "jvm", 100)], busy=400, total=800, usage=2_000_000, pss=300),
        _row(12, [(1, "jvm", 200)], busy=800, total=1600, usage=4_000_000, pss=200),
        _row(99, [(1, "jvm", 9999)], busy=1600, total=1600, usage=9_000_000),
    ]
    win = samples.window_summary(rows, 10, 12)
    assert win["samples"] == 3 and win["cpu_s_by_role"]["jvm"] == 2.0
    assert win["cpu_cores_mean"] == 1.0 and win["host_cpu_util"] == 0.5
    assert win["cgroup_cpu_s"] == 4.0 and win["cgroup_cores_mean"] == 2.0
    assert win["pss_peak_mb"] == 300.0
    assert samples.host_cpu_utilisation(rows[:1]) is None


class FakeClient:
    def __init__(self):
        self.n = 0
        self.closed = []
        self.acquired_at = []
        self.entered_at = []
        self.names = {}

    async def acquire_session(self, **kw):
        assert kw["reuse_dirty"] is False  # the SessionRef invariant
        self.n += 1
        n = self.n  # before the await: concurrent acquires share the counter
        await asyncio.sleep(0.01)
        self.acquired_at.append(time.time())
        return {"session_id": f"s{n}", "lease_id": f"l{n}"}

    async def close_session(self, sid, lease_id=None):
        self.closed.append(sid)
        return {}

    async def enter_theory(self, sid, name, *, imports=None, lease_id=None):
        assert name not in self.names.setdefault(sid, set())
        self.names[sid].add(name)
        self.entered_at.append(time.time())
        return {"success": True}

    async def verify_chunk(self, sid, chunk, timeout=None, *, lease_id=None):
        await asyncio.sleep(0.001)
        return {
            "success": True,
            "proof_open": False,
            "used_sorry": False,
            "timed_out": False,
            "commands": [{"status": "ok"}, {"status": "ok"}],
            "execution_time": 0.001,
        }


class FakeSampler:
    def __init__(self):
        self.phases = []

    def phase(self, name):
        self.phases.append((name, time.time()))


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


def test_run_cell_acquires_first_then_replays_and_closes(tmp_path):
    files = _corpus(tmp_path)
    census = _census(files, bad=("C",))
    spec = run_t1.CellSpec(
        workers=2,
        rep=1,
        seed=7,
        quota=2,
        heap="HOL",
        session_theories=["Main"],
        out_dir=tmp_path / "cell",
    )
    cl, smp = FakeClient(), FakeSampler()
    body = asyncio.run(run_t1.run_cell(cl, spec, files, census, smp))
    assert body["outcome"] == "ok" and body["excluded_stems"] == ["C"]
    assert body["assignment"] == corpus.assignment(["A", "B"], 7, 2, 2)
    assert body["window_start"] >= max(cl.acquired_at)  # clock after all acquires
    assert body["window_start"] <= min(cl.entered_at)  # and before the first enter
    assert [p[0] for p in smp.phases] == ["acquire", "run", "stop"]
    assert sorted(cl.closed) == ["s1", "s2"] and body["replays"] == 4
    assert body["nodes_per_session"] == {"0": 2, "1": 2}
    lines = (tmp_path / "cell" / "replays.jsonl").read_text().splitlines()
    assert len(lines) == 4 and all(json.loads(x)["blocks"] for x in lines)


def test_matrix_runner_order_resume_and_lock(tmp_path, monkeypatch, capsys):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("workers: [4, 1, 2]\nrepeats: 2\n")
    out = tmp_path / "results"
    assert list(run_matrix.cells({"workers": [4, 1, 2], "repeats": 2})) == [
        (1, 0),
        (1, 1),
        (2, 0),
        (2, 1),
        (4, 0),
        (4, 1),
    ]
    d = run_t1.cell_dir(out, 1, 0)
    d.mkdir(parents=True)
    (d / "manifest.json").write_text(json.dumps({"outcome": "ok"}))
    d = run_t1.cell_dir(out, 1, 1)
    d.mkdir(parents=True)
    (d / "manifest.json").write_text(json.dumps({"outcome": "error"}))
    ran = []
    monkeypatch.setattr(
        run_matrix, "run_one", lambda a, w, r: ran.append((w, r)) or "ok"
    )
    base = ["--config", str(cfg), "--out", str(out), "--workers", "1,2", "--reps", "2"]
    assert run_matrix.main(base) == 0
    assert ran == [(2, 0), (2, 1)]  # ok skipped, error skipped without --rerun-partial
    ran.clear()
    assert run_matrix.main(base + ["--rerun-partial"]) == 0 and (1, 1) in ran
    (out / "RUNNER_LOCK").write_text(f"{os.getpid()} now\n")
    assert run_matrix.main(base) == 2
    ns = run_matrix.argparse.Namespace(
        config="c",
        census="x.json",
        out="o",
        local_smoke=True,
        ips_external=True,
        url="http://h:1",
        quota=2,
    )
    cmd = run_matrix.child_cmd(ns, 8, 1)
    assert cmd[cmd.index("--workers") + 1] == "8" and "--local-smoke" in cmd
    assert cmd[-4:] == ["--url", "http://h:1", "--quota", "2"]
