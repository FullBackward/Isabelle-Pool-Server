"""memory-scaling stage 6: E2/E3/E4 with fake drivers (no tools)."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
from pathlib import Path

import httpx

sys.path.insert(
    0, str(Path(__file__).resolve().parents[1] / "evaluation" / "memory_scaling")
)

from memscale import experiments, workload  # noqa: E402


def _workload(tmp_path):
    thy = tmp_path / "W.thy"
    thy.write_text("theory W\n  imports Main\nbegin\n\nlemma a: True by simp\n\nend\n")
    return workload.load_workload(
        "HOL", str(thy), 'lemma memscale_p5_done: "True" by simp'
    )


class FakeIPS:
    name, topology = "ips", "pool"

    def __init__(self, refuse_at=None):
        self.started, self.loaded, self.released = [], [], 0
        self.refuse_at = refuse_at
        self.stopped = False
        self.cgroup_dir = None

    async def setup(self):
        pass

    async def start_session(self, i):
        if self.refuse_at is not None and i >= self.refuse_at and self.released == 0:
            resp = httpx.Response(
                503,
                json={"error": "memory_pressure", "state": "critical"},
                request=httpx.Request("POST", "http://x"),
            )
            raise httpx.HTTPStatusError("503", request=resp.request, response=resp)
        self.started.append(i)

    async def load(self, i, text):
        self.loaded.append(i)
        return {"ok": True}

    async def warm_hit(self, i):
        return {"warm_s": 0.5, "reused": True, "same_session": True}

    async def release_half(self):
        self.released += 1

    async def probe(self, i):
        self.last_probe = {"ok": i != 7, "error": None if i != 7 else "prover died"}
        return i != 7

    async def stop_all(self):
        self.stopped = True

    def root_pids(self):
        return []

    def describe(self):
        return {"name": "ips"}


def test_warm_hit_and_kill_probe(tmp_path, monkeypatch):
    w = _workload(tmp_path)
    body = asyncio.run(
        experiments.warm_hit(FakeIPS(), w, 3, tmp_path / "warm", rounds=2)
    )
    assert (
        body["cold_s_p50"] is not None
        and body["warm_s_p50"] == 0.5
        and body["reused_all"]
        and len(body["warm"]) == 6
    )
    assert (
        json.load(open(tmp_path / "warm" / "manifest.json"))["experiment"] == "warm_hit"
    )
    killed = []
    monkeypatch.setattr(experiments, "newest_session_poly", lambda roots: 4242)
    monkeypatch.setattr(experiments.os, "kill", lambda pid, sig: killed.append(pid))
    drv = FakeIPS()
    body = asyncio.run(experiments.kill_probe(drv, w, 8, tmp_path / "kill", answer_s=5))
    assert (
        killed == [4242]
        and body["victim_pid"] == 4242
        and body["others_answered"] is True
    )
    assert (
        body["answers"]["7"]["ok"] is False and drv.stopped and body["outcome"] == "ok"
    )
    assert body["answers"]["7"]["detail"]["error"] == "prover died"
    assert "detail" not in body["answers"]["0"]  # only a failed answer carries one


def test_pressure_samples_the_tools_own_scope(tmp_path, monkeypatch):
    """IPS runs in its own scope with MemoryMax: that cgroup is sampled and
    reported whatever the caller passed (the server run of 2026-10-21 reported
    the login session's cgroup, ~98 MB, for a 48 GB experiment)."""
    w = _workload(tmp_path)
    monkeypatch.setattr(experiments, "_dmesg_oom", lambda: [])
    seen = []

    def fake_cgroup_memory(d):
        seen.append(d)
        return {"current": None, "peak": None, "max": None, "oom_kill": 0}

    monkeypatch.setattr(experiments.procs, "cgroup_memory", fake_cgroup_memory)
    drv = FakeIPS(refuse_at=2)
    drv.cgroup_dir = "/sys/fs/cgroup/run-ips.scope"
    body = asyncio.run(
        experiments.pressure(
            drv,
            w,
            tmp_path / "p",
            max_sessions=5,
            recover_wait_s=5,
            cgroup_dir="/sys/fs/cgroup/caller.scope",
        )
    )
    assert body["cgroup_dir"] == "/sys/fs/cgroup/run-ips.scope"
    assert seen[-1] == "/sys/fs/cgroup/run-ips.scope"


def test_pressure_survives_sigterm_with_a_manifest(tmp_path, monkeypatch):
    """systemd stops the whole scope after a kernel OOM kill (OOMPolicy=stop),
    so the experiment got SIGTERM with its victim (server run 2026-10-21:
    'Terminated', no result). The handler records it and the manifest is
    still written, with the tool stopped."""
    w = _workload(tmp_path)
    monkeypatch.setattr(experiments, "_dmesg_oom", lambda: [])

    class Terminating(FakeIPS):
        async def load(self, i, text):
            if i == 1:
                os.kill(os.getpid(), signal.SIGTERM)
                await asyncio.sleep(2.0)  # the handler cancels us here
            return await super().load(i, text)

    drv = Terminating()
    body = asyncio.run(
        experiments.pressure(drv, w, tmp_path / "p", max_sessions=5, recover_wait_s=5)
    )
    m = json.load(open(tmp_path / "p" / "manifest.json"))
    assert body["outcome"] == "terminated" and m["outcome"] == "terminated"
    assert "SIGTERM" in body["stop_reason"] and body["sessions_before_stop"] == 1
    assert drv.stopped and "cgroup_at_end" in m


def test_pide_b_spawns_a_process_per_session_on_demand(monkeypatch):
    """E3 under topology B adds sessions past the k given at setup: a new MCP
    process per session, not an IndexError (server run of 2026-10-21)."""
    from memscale import drive_pide  # pylint: disable=import-outside-toplevel

    class FakeMCP:
        server_info = {}

        def __init__(self, tag):
            self.tag = tag

        async def call(self, name, args, timeout_s=0):
            return {"session": args.get("session")}

        async def close(self):
            pass

    spawned = []

    async def fake_spawn(self, tag):
        spawned.append(tag)
        return FakeMCP(tag)

    monkeypatch.setattr(drive_pide.PIDEDriver, "_spawn", fake_spawn)
    for topo, expect in (("B", 3), ("A", 1)):
        d = drive_pide.PIDEDriver(heap="HOL", topology=topo, k=1, command=["x"])
        d.tools = ["start_session", "create_scratch", "edit", "stop_session"]
        asyncio.run(d.setup())
        for i in range(3):
            asyncio.run(d.start_session(i))
        assert len(d.procs) == expect and d.session_ids == {0: "s0", 1: "s1", 2: "s2"}
    assert spawned == ["HOL-B0", "HOL-B1", "HOL-B2", "HOL-A0"]


def test_pressure_stops_at_the_refusal_and_measures_recovery(tmp_path, monkeypatch):
    w = _workload(tmp_path)
    monkeypatch.setattr(experiments, "_dmesg_oom", lambda: [])
    drv = FakeIPS(refuse_at=3)
    body = asyncio.run(
        experiments.pressure(drv, w, tmp_path / "p", max_sessions=10, recover_wait_s=5)
    )
    assert body["sessions_before_stop"] == 3 and body["stop_reason"].startswith(
        "http 503: memory_pressure"
    )
    assert (
        body["steps"][3]["http"] == 503
        and body["steps"][3]["body"]["error"] == "memory_pressure"
    )
    assert (
        drv.released == 1
        and body["recovery_s"] is not None
        and body["outcome"] == "ok"
        and drv.stopped
    )
    assert (tmp_path / "p" / "samples.jsonl").exists() and json.load(
        open(tmp_path / "p" / "manifest.json")
    )["steps"]
