"""Training metrics: preparing the corpus from the Isabelle under test (owner
decision 2026-10-22) and stopping a leftover server with everything under it
(review 5141144 S1). Real processes; the tree tests need Linux /proc."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(
    0, str(Path(__file__).resolve().parents[1] / "evaluation" / "training_metrics")
)

from trainmetrics import prepare_corpus, procctl  # noqa: E402

LINUX = os.path.exists("/proc/self/stat")


def test_prepare_corpus_rewrites_imports_and_records_a_manifest(tmp_path):
    src = tmp_path / "Analysis"
    src.mkdir()
    (src / "Base.thy").write_text(
        "theory Base\n  imports Main\nbegin\nlemma b: True by simp\nend\n"
    )
    (src / "Top.thy").write_text(
        'theory Top\n  imports Base "HOL-Library.FSet"\nbegin\nlemma t: True by simp\nend\n'
    )
    (src / "Other.thy").write_text("theory Other imports Main begin end\n")
    out = tmp_path / "corpus"
    (out / "processed").mkdir(parents=True)
    (out / "processed" / "Stale.thy").write_text("old")
    m = prepare_corpus.prepare(src, ["Top", "Base", "Gone"], out, {"isabelle_id": "x"})
    assert sorted(p.name for p in (out / "processed").glob("*.thy")) == [
        "Base.thy",
        "Top.thy",
    ]
    top = (out / "processed" / "Top.thy").read_text()
    assert (
        "HOL-Analysis.Base" in top and "HOL-Library.FSet" in top
    )  # local import qualified
    assert m["missing"] == ["Gone"] and [f["stem"] for f in m["files"]] == [
        "Top",
        "Base",
    ]
    assert len(m["corpus_sha256"]) == 64 and m["isabelle_id"] == "x"
    on_disk = json.loads((out / "MANIFEST.json").read_text())
    assert on_disk["corpus_sha256"] == m["corpus_sha256"]


def test_the_stems_file_lists_the_94_theories():
    stems = prepare_corpus.read_stems()
    assert len(stems) == 94 and len(set(stems)) == 94
    assert {"Sum_Topology", "Integral_Test", "Uncountable_Sets"} <= set(stems)


def test_cgroup_kill_and_busy(tmp_path):
    cg = tmp_path / "run-x.scope"
    cg.mkdir()
    (cg / "cgroup.kill").write_text("")
    (cg / "cgroup.procs").write_text("123\n")
    assert procctl.cgroup_busy(str(cg)) and not procctl.cgroup_busy(None)
    assert procctl.cgroup_kill(str(cg)) and (cg / "cgroup.kill").read_text() == "1"
    assert not procctl.cgroup_kill(str(tmp_path / "missing"))


SERVER = r"""
import os, signal, subprocess, sys, time
signal.signal(signal.SIGTERM, signal.SIG_IGN)   # a hung server: ignores SIGTERM
jvm = subprocess.Popen([sys.executable, "-c",
    "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(300)"],
    start_new_session=True)                     # like the gateway JVM: another session
print(jvm.pid, flush=True)
time.sleep(300)
"""


@pytest.mark.skipif(not LINUX, reason="needs /proc for the process tree")
def test_kill_server_stops_the_whole_tree_across_sessions():
    srv = subprocess.Popen(
        [sys.executable, "-c", SERVER], stdout=subprocess.PIPE, start_new_session=True
    )
    jvm = int(srv.stdout.readline())
    tree = procctl.group_tree(srv.pid)
    assert {srv.pid, jvm} <= tree  # the other session's child is in the tree
    os.killpg(srv.pid, signal.SIGKILL)  # what the old code did: the group only
    srv.wait()
    assert procctl._alive([jvm])  # the "JVM" survived in its own session
    done = procctl.kill_server(srv.pid, tree, None, grace_s=1.0)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and procctl._alive([jvm]):
        time.sleep(0.2)
    assert not procctl._alive([jvm]) and done["sigkilled"] >= 1


def test_cgroup_kill_refuses_the_runners_own_cgroup_and_its_ancestors(
    tmp_path, monkeypatch
):
    """Review afd3de9 S2."""
    for d in ("slice", "slice/run.scope", "other.scope"):
        (tmp_path / d).mkdir(parents=True, exist_ok=True)
        (tmp_path / d / "cgroup.kill").write_text("")
    monkeypatch.setattr(
        procctl, "_own_cgroup", lambda: str(tmp_path / "slice" / "run.scope")
    )
    assert not procctl.cgroup_kill(str(tmp_path / "slice" / "run.scope"))  # its own
    assert not procctl.cgroup_kill(str(tmp_path / "slice") + "/")  # an ancestor
    assert procctl.cgroup_kill(str(tmp_path / "other.scope"))  # a sibling scope
    assert (tmp_path / "slice" / "cgroup.kill").read_text() == ""
