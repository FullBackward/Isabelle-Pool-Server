"""Tests for the LSP-MCP comparison runner (run_isabelle_pool_server_lsp.py).

Unit-level tests import the runner WITHOUT openai/mcp (the runner imports those
lazily). The mock-LLM smoke needs mcp + openai + a running Isabelle Pool Server HTTP
server — it skips cleanly when any is missing.
"""
from __future__ import annotations

import dataclasses
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

# repo root (for common.*) and evaluation/MCP-comparison (for the runner module)
_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO))
sys.path.insert(0, str(_REPO / "evaluation" / "MCP-comparison"))

import run_isabelle_pool_server_lsp as runner


# ------------------------------------------------------- local file tools

def _workdir(tmp_path: Path) -> Path:
    wd = tmp_path / "work"
    wd.mkdir()
    (wd / "Mini.thy").write_text(
        'theory Mini imports Main begin\ntheorem mini: "True" sorry\nend\n', encoding="utf-8")
    return wd


def test_workdir_sandbox_rejects_escape(tmp_path):
    wd = _workdir(tmp_path)
    with pytest.raises(ValueError):
        runner._safe_workdir_path(wd, "/etc/passwd")
    with pytest.raises(ValueError):
        runner._safe_workdir_path(wd, "../outside.thy")
    # None (no path arg) and in-workdir paths are fine
    assert runner._safe_workdir_path(wd, None) == wd.resolve()
    assert runner._safe_workdir_path(wd, "Mini.thy") == (wd / "Mini.thy").resolve()


def test_file_tools_read_write_roundtrip(tmp_path):
    wd = _workdir(tmp_path)
    assert "sorry" in runner.handle_local_tool(wd, "read_file", {})
    out = runner.handle_local_tool(wd, "write_file", {"content": "theory Mini imports Main begin\ntheorem mini: \"True\" by (rule TrueI)\nend\n"})
    assert "wrote" in out
    assert "rule TrueI" in runner.handle_local_tool(wd, "read_file", {})


def test_file_tools_validate_input(tmp_path):
    wd = _workdir(tmp_path)
    assert "ERROR" in runner.handle_local_tool(wd, "write_file", {"content": "  "})
    assert "ERROR" in runner.handle_local_tool(wd, "write_file", {})
    assert "ERROR" in runner.handle_local_tool(wd, "definitely_not_a_tool", {})


def test_tool_surface_is_mcp_plus_file_tools():
    assert [t["name"] for t in runner.FILE_TOOLS] == ["read_file", "write_file"]
    assert runner.SYSTEM == "isabelle_pool_server_lsp"


# ------------------------------------------------------- mock-LLM smoke

_GOOD = 'theory Mini imports Main begin\ntheorem mini: "True" by (rule TrueI)\nend\n'


class _FakeTC:
    def __init__(self, name: str, args: dict, i: int):
        self.id = f"call_{i}"
        self.function = SimpleNamespace(name=name, arguments=json.dumps(args))


class _ScriptedModel:
    """Fake ModelClient: round 1 writes the known-good proof, round 2 says DONE."""

    def __init__(self, content: str):
        self.calls = 0
        self._content = content

    async def chat(self, messages, tools=None, system_prompt=None):
        self.calls += 1
        if self.calls == 1:
            return SimpleNamespace(
                assistant_text="writing the proof",
                tool_calls=[_FakeTC("write_file", {"content": self._content}, 1)],
                usage={}, finish_reason="tool_calls", reasoning_text=None)
        return SimpleNamespace(
            assistant_text="DONE", tool_calls=[], usage={},
            finish_reason="stop", reasoning_text=None)


def _server_up() -> bool:
    try:
        import httpx
        return httpx.get("http://localhost:8000/readyz", timeout=5).status_code == 200
    except Exception:
        return False


@pytest.mark.skipif(not _server_up(), reason="needs the Isabelle Pool Server on :8000")
def test_mock_llm_solves_through_arbiter(tmp_path):
    """Scripted model: write_file(good proof) → DONE. Asserts the full path —
    stdio MCP spawn, file tools, sync, DONE gate, arbiter — yields a solved row."""
    pytest.importorskip("mcp")
    pytest.importorskip("openai")
    import asyncio

    from common.config import load
    from common.problems import parse_thy
    from common.metrics import load_results

    wd = _workdir(tmp_path)
    problem = parse_thy(wd / "Mini.thy")

    cfg = dataclasses.replace(
        load(),
        paths=dataclasses.replace(load().paths, runs_dir=tmp_path / "runs"),
    )
    results_path = tmp_path / "results.jsonl"
    asyncio.run(runner.run_attempt(
        problem, 0, results_path, client=_ScriptedModel(_GOOD), cfg=cfg))

    rows = load_results(results_path)
    assert len(rows) == 1
    assert rows[0].system == "isabelle_pool_server_lsp"
    assert rows[0].arbiter_solved is True
    assert rows[0].agent_claimed_solved is True
    assert rows[0].error is None
