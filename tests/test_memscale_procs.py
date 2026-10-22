"""memory-scaling experiments, stage 1 (plan 2026-10-20): the /proc readers,
the sampler and the workload helpers against a fake /proc tree."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(
    0, str(Path(__file__).resolve().parents[1] / "evaluation" / "memory_scaling")
)

from memscale import procs, sampler, workload  # noqa: E402


def _fake_proc(
    tmp_path,
    table,
    mem,
    meminfo=("MemTotal: 134217728 kB", "MemAvailable: 100000000 kB"),
):
    """table: {pid: (ppid, comm, state, cmdline)}; mem: {pid: (rss_kb, pss_kb, priv_kb, shared_kb)}"""
    root = tmp_path / "proc"
    root.mkdir()
    for pid, (ppid, comm, state, cmd) in table.items():
        d = root / str(pid)
        d.mkdir()
        (d / "stat").write_text(
            f"{pid} ({comm}) {state} {ppid} {pid} 1 0 -1 0 0 0 0 0 0 0 0 0\n"
        )
        (d / "cmdline").write_text(cmd.replace(" ", "\0") + "\0")
        if pid in mem:
            rss, pss, priv, shared = mem[pid]
            (d / "smaps_rollup").write_text(
                f"00400000-7fff Rss: {rss} kB\nRss:  {rss} kB\nPss:  {pss} kB\n"
                f"Private_Clean: 0 kB\nPrivate_Dirty: {priv} kB\nShared_Clean: {shared} kB\nShared_Dirty: 0 kB\n"
            )
    (root / "meminfo").write_text("\n".join(meminfo) + "\n")
    (root / "self").mkdir()
    (root / "self" / "cgroup").write_text("0::/user.slice/exp.scope\n")
    cg = tmp_path / "cgroup" / "user.slice" / "exp.scope"
    cg.mkdir(parents=True)
    (cg / "memory.current").write_text("123456\n")
    (cg / "memory.peak").write_text("234567\n")
    (cg / "memory.max").write_text("max\n")
    (cg / "memory.events").write_text("low 0\nhigh 0\nmax 0\noom 0\noom_kill 2\n")
    return root, tmp_path / "cgroup"


TABLE = {
    1: (0, "systemd", "S", "/sbin/init"),
    100: (
        1,
        "python",
        "S",
        "python -m server.app.main",
    ),  # IPS server (root by pattern)
    101: (100, "bash", "S", "bash /opt/isabelle/bin/isabelle scala x"),
    102: (101, "java", "S", "java -cp ... isabelle.Scala"),  # gateway JVM
    103: (102, "bash_process", "S", "bash_process"),
    104: (103, "poly", "S", "poly -q --minheap 500"),  # session poly
    105: (103, "poly", "Z", "poly"),  # a zombie: no memory, skipped
    200: (
        1,
        "bash",
        "S",
        "bash /opt/isabelle/bin/isabelle pide_mcp -l HOL",
    ),  # PIDE MCP (root by pattern)
    201: (200, "java", "S", "java isabelle.pide.mcp.PIDE_MCP"),
    202: (201, "poly", "S", "poly -q"),
    300: (1, "java", "S", "java SomethingElse"),  # a stranger
}
MEM = {
    100: (50_000, 50_000, 50_000, 0),
    101: (3_000, 1_000, 500, 2_500),
    102: (3_000_000, 2_900_000, 2_800_000, 200_000),
    103: (1_000, 500, 200, 800),
    104: (1_200_000, 900_000, 600_000, 600_000),
    200: (3_000, 1_000, 500, 2_500),
    201: (1_500_000, 1_400_000, 1_300_000, 200_000),
    202: (1_100_000, 800_000, 500_000, 600_000),
    300: (999_999, 999_999, 999_999, 0),
}


@pytest.fixture
def fake(tmp_path, monkeypatch):
    root, cg = _fake_proc(tmp_path, TABLE, MEM)
    monkeypatch.setattr(procs, "PROC_ROOT", str(root))
    monkeypatch.setattr(procs, "CGROUP_ROOT", str(cg))
    return root


def test_process_table_and_roots(fake):
    table = procs.process_table()
    assert table[104] == (103, "poly", "S") and table[105][2] == "Z"
    assert procs.find_roots(["server.app.main"], table) == {100}
    assert procs.find_roots(["pide_mcp"], table) == {200}
    assert procs.find_roots([], table) == set()
    assert procs.descendants([100], table) == {100, 101, 102, 103, 104, 105}
    assert procs.descendants([4242], table) == set()


def test_snapshot_sums_pss_by_role_and_skips_zombies(fake):
    rows = procs.snapshot([100])
    assert [r.pid for r in rows] == [100, 101, 102, 103, 104]  # 105 is a zombie
    by = {r.pid: r for r in rows}
    assert by[104].role == "poly" and by[102].role == "jvm" and by[103].role == "other"
    assert (
        by[104].pss == 900_000 * 1024
        and by[104].uss == 600_000 * 1024
        and by[104].shared == 600_000 * 1024
    )
    sums = procs.sum_rows(rows)
    assert sums["total"]["pss"] == (50_000 + 1_000 + 2_900_000 + 500 + 900_000) * 1024
    assert sums["jvm"]["pss"] == 2_900_000 * 1024 and sums["poly"]["count"] == 1
    # the stranger JVM (300) is not under any root
    assert all(r.pid != 300 for r in rows)


def test_host_and_cgroup_memory(fake):
    host = procs.host_memory()
    assert (
        host["total"] == 134217728 * 1024
        and host["used"] == (134217728 - 100000000) * 1024
    )
    cg = procs.cgroup_memory()
    assert cg == {"current": 123456, "peak": 234567, "max": None, "oom_kill": 2}


def test_sampler_writes_rows_with_phase_and_patterns(fake, tmp_path):
    """Count-based (plan review 2026-10-22 S1): explicit samples, no thread, so
    the assertions do not depend on the 1 s interval under load."""
    out = tmp_path / "samples.jsonl"
    s = sampler.Sampler(
        str(out), patterns=["server.app.main", "pide_mcp"], interval_s=0.02
    )
    s.phase("P1")
    s.add_roots([300])
    s.phase("P2")
    s.sample_once()
    rows = sampler.load_rows(str(out))
    assert rows[0]["phase"] == "P1" and rows[0]["roots"] == [100, 200]
    p2 = [r for r in rows if r["phase"] == "P2"]
    assert p2 and p2[0]["roots"] == [100, 200, 300]
    assert (
        p2[0]["sum"]["total"]["pss"] > rows[0]["sum"]["total"]["pss"]
    )  # the stranger counted now
    assert p2[0]["host"]["used"] > 0 and p2[0]["cgroup"]["oom_kill"] == 2
    assert {p["role"] for p in p2[0]["procs"]} >= {"jvm", "poly", "python"}
    assert s.rows_written == len(rows) == 3
    json.dumps(rows[0])  # serialisable


def test_sampler_thread_writes_rows(fake, tmp_path):
    out = tmp_path / "samples.jsonl"
    with sampler.Sampler(str(out), patterns=["server.app.main"], interval_s=0.02) as s:
        s.phase("P1")
    rows = sampler.load_rows(str(out))
    assert rows and s.rows_written == len(rows) and s.errors == 0


def test_cpu_columns(fake, tmp_path):
    """Per-process utime+stime and host busy/total ticks (training-metrics
    stage 1); new keys only, the memory columns are unchanged."""
    proc_root = Path(procs.PROC_ROOT)
    (proc_root / "200" / "stat").write_text(
        "200 (bash) S 1 200 1 0 -1 0 0 0 0 0 750 250 0 0\n"
    )
    (proc_root / "stat").write_text(
        "cpu  1000 0 500 8000 500 0 0 0 0 0\ncpu0 1 0 0 0 0 0 0 0 0 0\n"
    )
    assert procs.cpu_ticks(200) == 1000 and procs.cpu_ticks(99999) is None
    assert procs.host_cpu() == {"busy": 1500, "total": 10000}
    assert procs.clk_tck() > 0
    row = sampler.Sampler(str(tmp_path / "s.jsonl"), root_pids=[200]).sample_once()
    by_pid = {p["pid"]: p for p in row["procs"]}
    assert by_pid[200]["cpu_ticks"] == 1000 and row["host_cpu"]["total"] == 10000
    assert row["sum"]["total"]["cpu_ticks"] >= 1000 and row["clk_tck"] > 0


def test_cgroup_cpu(fake, tmp_path):
    """Review a66fa6e S2: the scope's cpu.stat, exact CPU of everything in it."""
    cg = Path(procs.CGROUP_ROOT) / "user.slice" / "exp.scope"
    (cg / "cpu.stat").write_text(
        "usage_usec 5000000\nuser_usec 4000000\nsystem_usec 1000000\nnr_periods 0\n"
    )
    assert procs.cgroup_cpu() == {
        "usage_usec": 5000000,
        "user_usec": 4000000,
        "system_usec": 1000000,
    }
    assert procs.cgroup_cpu(str(tmp_path / "nowhere"))["usage_usec"] is None
    row = sampler.Sampler(str(tmp_path / "s.jsonl"), root_pids=[200]).sample_once()
    assert row["cgroup_cpu"]["usage_usec"] == 5000000


def test_workload_helpers(tmp_path):
    thy = tmp_path / "W.thy"
    thy.write_text(
        '(* c *)\ntheory W\n  imports Main "HOL-Library.Multiset"\nbegin\n\nlemma a: True by simp\n\nend\n'
    )
    w = workload.load_workload("HOL-Library", str(thy), 'lemma z: "True" by simp')
    assert w.theory_name == "W" and w.imports == ("Main", "HOL-Library.Multiset")
    assert len(w.sha256) == 64 and w.as_manifest()["lines"] == 9
    renamed = workload.with_theory_name(w.text, "W_3")
    assert "theory W_3\n" in renamed and workload.parse_header(renamed)[0] == "W_3"
    body = workload.body_before_end(w.text)
    assert body.rstrip().endswith("by simp") and "\nend" not in body


def test_find_roots_never_matches_the_sampler_chain_and_flags_gaps(fake, monkeypatch):
    """Review 2e1935e S1–S3: the sampler's own chain is never a root even when
    its argv names the pattern; an unreadable live process is listed in
    skipped_pids; a missing Pss line is flagged, not silently used."""
    table = procs.process_table()
    (fake / "1" / "cmdline").write_text(
        "bash -c python -m memscale --pattern server.app.main\0"
    )
    monkeypatch.setattr(procs.os, "getpid", lambda: 300)  # we ARE pid 300, child of 1
    assert procs.self_chain(table) == {300, 1}
    assert procs.find_roots(["server.app.main"], table) == {
        100
    }  # pid 1 matches but is our ancestor
    (fake / "101" / "smaps_rollup").unlink()
    skipped = []
    rows = procs.snapshot([100], table, skipped)
    assert skipped == [101] and 101 not in {r.pid for r in rows}
    (fake / "103" / "smaps_rollup").write_text("Rss: 1000 kB\nPrivate_Dirty: 200 kB\n")
    row = next(r for r in procs.snapshot([100], table) if r.pid == 103)
    assert row.pss == row.rss == 1000 * 1024 and row.pss_estimated is True
    s = sampler.Sampler(str(fake.parent / "x.jsonl"), root_pids=[100])
    r = s.sample_once()
    assert (
        r["skipped_pids"] == [101]
        and r["pss_estimated"] == 1
        and r["errors"] == 0
        and r["cgroup_dir"]
    )
