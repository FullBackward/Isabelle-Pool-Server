"""The LSP MCP server object and the plumbing every tool module shares:
``mcp``, ``pool``, the memory-protocol tool names, the per-tool guard and the
``tool`` / ``synced_tool`` decorators. Tool modules (app.py, memory_tools.py)
import from here; nothing here imports them (split out of app.py at the
source-size gate — review d2bc679 S3).
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable, TypeVar

from mcp.server.fastmcp import FastMCP

from ..common import ToolNames, make_guard
from .config import Config
from .pool import LspPool

mcp = FastMCP("isabelle-pool-server-lsp", host=Config.HOST, port=Config.PORT)
pool = LspPool()

# Memory protocol (plan §2.C, owner decisions D3/D4): the server reports FACTS
# (503 bodies with an `error` key, the X-Memory-State header, /api/v1/memory);
# this server words the advice and names its own tools. Every tool is wrapped:
# an HTTP error becomes structured JSON with advice; while the server is at
# warning/critical a MEMORY REMINDER is attached to results and the idle warm
# scratch sessions are closed first (the MCP frees its own before asking).
TOOLS = ToolNames(
    release_idle="isabelle_release_idle()",
    close="isabelle_close(file_path, destroy=true)",
    list_sessions="isabelle_list_sessions()",
    memory_status="isabelle_memory_status()",
    reopen="the next file-scoped call reopens it automatically",
)
_guard = make_guard(
    registry=pool.registry,
    tools=TOOLS,
    get_client=pool.client,
    owner_of=lambda args, kwargs: None,
    idle_s=Config.ADVICE_IDLE_S,
    # Retire idle warm scratch sessions by tier (review d2bc679 S1): at
    # warning only those idle >= ADVICE_IDLE_S, at critical every idle one.
    on_reminder=lambda state: pool.retire_idle_scratch(
        0.0 if state == "critical" else float(Config.ADVICE_IDLE_S)
    ),
)


_F = TypeVar("_F", bound=Callable[..., Awaitable[Any]])


def tool(**kw: Any) -> Callable[[_F], _F]:
    """`@mcp.tool()` + the memory guard (see TOOLS)."""

    def deco(fn: _F) -> _F:
        return mcp.tool(**kw)(_guard(fn))  # type: ignore[no-any-return]

    return deco


_SYNC_NOTE = (
    "The file is re-read from disk and re-synced to its session before "
    "answering if it changed (the MCP never writes files). Positions are "
    "1-based line/col, UTF-16 columns."
)


def synced_tool(**kw):
    """`@mcp.tool()` for file-scoped tools: appends _SYNC_NOTE to the REAL
    docstring. (A triple-quoted string + _SYNC_NOTE EXPRESSION is not a docstring — the
    function's __doc__ was None and the tool shipped with an EMPTY description,
    i.e. no instructions for the agent — caught by tests/test_mcp_tools_smoke.py.)"""

    def deco(fn):
        fn.__doc__ = (fn.__doc__ or "").rstrip() + " " + _SYNC_NOTE
        return mcp.tool(**kw)(_guard(fn))

    return deco


async def _bound_synced(file_path: str):
    """get_binding + sync — the preamble of every file-scoped tool."""
    binding = await pool.get_binding(file_path)
    reloaded = await pool.sync(binding)
    return binding, reloaded
