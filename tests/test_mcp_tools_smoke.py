"""Tracker TEST-1 (audit P0 item 10): spawn each MCP server IN-PROCESS and assert
its tool surface. No Isabelle Pool Server is contacted — the servers are
only instantiated, and tools are listed, not called.

Guards against the failure class DOC-1 exposed (a renamed/lost module or tool
that every onboarding path and the comparison runner silently depended on),
and enforces DESIGN_CHOICES 2.8: every tool description is part of the agent's
system prompt, so none may be empty. Skips when the `mcp` package is absent
(host env); runs in the container.
"""
from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("mcp", reason="mcp package not installed (container-only test)")

from mcp_servers.lsp import app as lsp_app  # noqa: E402
from mcp_servers.stepwise import app as stepwise_app  # noqa: E402

STEPWISE_TOOLS = {
    "enter_theory", "verify_chunk", "proof_state", "source", "diagnostic",
    "sledgehammer", "checkpoint", "restore", "rollback", "close_theory",
    "verify_batch",
}
LSP_TOOLS = {
    "isabelle_open", "isabelle_close", "isabelle_sync",
    "isabelle_diagnostic_messages", "isabelle_goal", "isabelle_command_at_line",
    "isabelle_proof_state", "isabelle_source", "isabelle_query",
    "isabelle_local_facts", "isabelle_global_facts", "isabelle_hover_info",
    "isabelle_definition", "isabelle_sledgehammer", "isabelle_checkpoint",
    "isabelle_restore", "isabelle_rollback", "isabelle_history",
    "isabelle_last_report", "isabelle_multi_attempt", "isabelle_run_code",
    "isabelle_build_heap", "isabelle_heap_status",
}


def _tools(server):
    return asyncio.run(server.list_tools())


def test_stepwise_server_lists_its_tools():
    tools = _tools(stepwise_app.mcp)
    assert {t.name for t in tools} == STEPWISE_TOOLS


def test_lsp_server_lists_its_tools():
    tools = _tools(lsp_app.mcp)
    assert {t.name for t in tools} == LSP_TOOLS


@pytest.mark.parametrize("server", [stepwise_app.mcp, lsp_app.mcp], ids=["stepwise", "lsp"])
def test_every_tool_has_a_description_and_schema(server):
    for t in _tools(server):
        assert (t.description or "").strip(), f"{t.name}: empty description (2.8: docstrings are the prompt)"
        assert isinstance(t.inputSchema, dict) and t.inputSchema.get("type") == "object", t.name


def test_stepwise_prompt_registered():
    prompts = asyncio.run(stepwise_app.mcp.list_prompts())
    assert {p.name for p in prompts} == {"prove_theorem"}


def test_verify_chunk_docstring_states_the_proved_rule():
    """`success=True ≠ proved` is the one invariant agents must never lose (2.4/2.8)."""
    desc = next(t.description for t in _tools(stepwise_app.mcp) if t.name == "verify_chunk")
    assert "proof_open" in desc and "used_sorry" in desc
