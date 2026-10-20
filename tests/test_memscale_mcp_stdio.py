"""memory-scaling stage 3: the minimal stdio MCP client against a fake
newline-JSON-RPC server (a python child): handshake, tools/list, tools/call
with structuredContent, a result larger than asyncio's default 64 KiB line
limit (the stage-3 smoke hang), isError -> MCPError, and close()."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(
    0, str(Path(__file__).resolve().parents[1] / "evaluation" / "memory_scaling")
)

from memscale.mcp_stdio import MCPError, StdioMCP  # noqa: E402

FAKE = r"""
import json, sys
for line in sys.stdin:
    msg = json.loads(line)
    m, i, p = msg.get("method"), msg.get("id"), msg.get("params") or {}
    if m == "initialize":
        out = {"protocolVersion": p["protocolVersion"], "serverInfo": {"name": "fake", "version": "1"}, "capabilities": {}}
    elif m == "notifications/initialized":
        continue
    elif m == "tools/list":
        out = {"tools": [{"name": "edit"}, {"name": "get_state"}]}
    elif m == "tools/call":
        name, args = p["name"], p.get("arguments") or {}
        if name == "big":
            out = {"content": [{"type": "text", "text": "x" * args["n"]}], "structuredContent": {"n": args["n"], "pad": "y" * args["n"]}}
        elif name == "bad":
            out = {"content": [{"type": "text", "text": "boom"}], "isError": True}
        elif name == "text":
            out = {"content": [{"type": "text", "text": json.dumps({"k": 1})}]}
        else:
            out = {"structuredContent": {"echo": args}}
    else:
        continue
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": i, "result": out}) + "\n"); sys.stdout.flush()
"""


@pytest.fixture
def fake_server(tmp_path):
    script = tmp_path / "fake_mcp.py"
    script.write_text(FAKE)
    return [sys.executable, "-u", str(script)]


def test_stdio_client_roundtrip_and_large_lines(fake_server, tmp_path):
    async def go():
        async with StdioMCP(fake_server, stderr_path=str(tmp_path / "err.log")) as m:
            assert m.pid and m.server_info["name"] == "fake"
            assert [t["name"] for t in await m.list_tools()] == ["edit", "get_state"]
            assert await m.call("edit", {"a": 1}) == {"echo": {"a": 1}}
            assert await m.call("text", {}) == {"k": 1}  # text content parsed as JSON
            big = await m.call(
                "big", {"n": 300_000}, timeout_s=30
            )  # > 64 KiB on one line
            assert big["n"] == 300_000 and len(big["pad"]) == 300_000
            with pytest.raises(MCPError):
                await m.call("bad", {})
            assert await m.call("edit", {"after": "error"}) == {
                "echo": {"after": "error"}
            }  # still alive
        assert m.proc is None

    asyncio.run(go())


def test_stdio_client_timeout_and_dead_server(tmp_path):
    async def go():
        m = StdioMCP(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            stderr_path=str(tmp_path / "e.log"),
        )
        with pytest.raises(MCPError):
            await m.start(init_timeout_s=0.5)
        await m.close()

    asyncio.run(go())


FAKE_PIDE = r"""
import json, sys
polls = 0
for line in sys.stdin:
    msg = json.loads(line); m, i, p = msg.get("method"), msg.get("id"), msg.get("params") or {}
    if m == "initialize": out = {"protocolVersion": p["protocolVersion"], "serverInfo": {"name": "pide-fake", "version": "1"}, "capabilities": {}}
    elif m == "notifications/initialized": continue
    elif m == "tools/list": out = {"tools": [{"name": n} for n in ("start_session", "create_scratch", "edit", "get_state", "stop_session")]}
    elif m == "tools/call":
        name, args = p["name"], p.get("arguments") or {}
        if name == "start_session": out = {"structuredContent": {"session": "sid-" + args["session"]}}
        elif name == "create_scratch": out = {"structuredContent": {"path": "/tmp/x/tmp_pide_mcp_scratch_" + args["name_suffix"] + ".thy", "message": "File created"}}
        elif name == "edit":
            assert args["text"].startswith("theory tmp_pide_mcp_scratch_memscale_"), args["text"][:40]
            out = {"structuredContent": {"status": "written", "description": "Edited 1 occurrence(s)"}}
        elif name == "get_state":
            polls += 1
            running = 2 if polls < 3 else 0
            out = {"structuredContent": {"commands": {"count": 288}, "commands_still_running_possibly_nonterminating": {"count": running, "commands": []}, "commands_unprocessed": running, "commands_failed": {"count": 0, "commands": []}, "commands_bad": 0, "commands_finished": 288 - 2 * running, "total_timing_ms": 1234}}
        elif name == "stop_session": out = {"structuredContent": {"stopped": args["sessions"], "remaining": {}}}
        else: out = {"content": [{"type": "text", "text": "unknown"}], "isError": True}
    else: continue
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": i, "result": out}) + "\n"); sys.stdout.flush()
"""


def test_pide_driver_against_a_fake_server(tmp_path, monkeypatch):
    """Stage 3: start_session / create_scratch / edit with the theory renamed
    to the scratch stem / get_state polled until nothing runs / stop_session;
    topology B spawns one process per session."""
    from memscale import drive_pide

    script = tmp_path / "fake_pide.py"
    script.write_text(FAKE_PIDE)
    monkeypatch.setattr(
        drive_pide.PIDEDriver, "poll_s", 0.0
    )  # no 1 s polls in the test

    async def go(topology):
        d = drive_pide.PIDEDriver(
            heap="HOL",
            topology=topology,
            k=2,
            command=[sys.executable, "-u", str(script)],
            log_dir=str(tmp_path / "logs"),
            load_timeout_s=5,
        )
        await d.setup()
        assert len(d.root_pids()) == (1 if topology == "A" else 2)
        await d.start_session(0)
        await d.start_session(1)
        assert d.session_ids == {0: "sid-s0", 1: "sid-s1"}
        res = await d.load(
            0, "theory CTL_0\n  imports Main\nbegin\n\nlemma a: True by simp\n\nend\n"
        )
        assert (
            res["ok"]
            and res["commands"]["count"] == 288
            and res["commands"]["polls"] >= 1
        )
        res2 = await d.recheck(1, "theory CTL_1 imports Main begin end")
        assert res2["ok"] and d.scratch[1].endswith("memscale_1.thy")
        desc = d.describe()
        assert desc["topology"] == topology and len(desc["processes"]) == (
            1 if topology == "A" else 2
        )
        await d.stop_all()
        assert d.root_pids() == [] and d.session_ids == {}

    asyncio.run(go("A"))
    asyncio.run(go("B"))


def test_dead_server_write_and_no_commands(tmp_path, monkeypatch):
    """13c06cf S2: writing to a crashed server raises MCPError (not a raw
    BrokenPipeError); S1: the stderr log handle is closed by close();
    S4: a theory reporting zero commands stops the settle poll early."""
    from memscale import drive_pide

    async def go():
        m = StdioMCP(
            [sys.executable, "-c", "import sys; sys.stdin.readline(); sys.exit(0)"],
            stderr_path=str(tmp_path / "e.log"),
        )
        (
            await m.start(init_timeout_s=5) if False else None
        )  # the fake exits after one line: initialize cannot complete
        return m

    async def go2():
        m = StdioMCP(
            [sys.executable, "-u", str(tmp_path / "fake_pide.py")],
            stderr_path=str(tmp_path / "e.log"),
        )
        await m.start()
        assert m._stderr_fh is not None and not m._stderr_fh.closed
        m.proc.kill()
        await m.proc.wait()
        with pytest.raises(MCPError):
            await m.call("edit", {})
        await m.close()
        assert m._stderr_fh is None

    (tmp_path / "fake_pide.py").write_text(FAKE_PIDE)
    asyncio.run(go2())

    class EmptyMCP:
        async def call(self, name, args, timeout_s=0):
            return {"commands": {"count": 0}}

    d = drive_pide.PIDEDriver(heap="HOL", topology="A", k=1, command=["x"])
    d.poll_s = 0.0
    d.procs = [EmptyMCP()]  # type: ignore[list-item]
    d.session_ids = {0: "s"}
    d.scratch = {0: "/tmp/x.thy"}
    state, polls = asyncio.run(d._settled(0, 10**9))
    assert state.get("no_commands") and polls == d.empty_polls
