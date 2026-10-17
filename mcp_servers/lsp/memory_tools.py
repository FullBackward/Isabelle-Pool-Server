"""Memory tools of the LSP MCP server (plan §2.C): what this MCP holds, the
prover server's memory state with advice, and the release lever. Registered on
the shared ``mcp`` from server.py when app.py imports this module."""

from __future__ import annotations

from ..common import advise
from ..common import dump_json as _j
from ..common import others_summary
from .config import Config
from .server import TOOLS, pool, tool


@tool()
async def isabelle_list_sessions() -> str:
    """The prover sessions this MCP holds: open files (bindings) and warm
    scratch sessions, each with idle time, when the server will reap it, and
    the memory attributed to it (refreshed from the server).

    `ref` is display-only: no tool takes it — act by file path
    (isabelle_close) or with isabelle_release_idle(). Use together with
    isabelle_memory_status() after a memory_pressure error or a MEMORY REMINDER."""
    c = await pool.client()
    try:
        facts = await c.memory_status()
        pool.registry.merge(facts.get("sessions") or [])
        pool.registry.mark_missing(facts.get("sessions") or [])
    except (
        Exception
    ):  # pylint: disable=broad-exception-caught  # local view still useful
        pass
    return _j(
        {
            "sessions": [e.to_agent() for e in pool.registry.all()],
            "memory_state": c.last_memory_state,
        }
    )


@tool()
async def isabelle_memory_status() -> str:
    """Prover-server memory: state (ok / warning / critical), used vs limit,
    what this MCP holds, what others hold (sessions, task groups, builds,
    external pressure), and advice when memory is tight. Call it after a
    memory_pressure error or a MEMORY REMINDER; if the advice says the memory
    is held by others, stop and tell the user."""
    c = await pool.client()
    facts = await c.memory_status()
    pool.registry.merge(facts.get("sessions") or [])
    owned = pool.registry.all()
    state = facts.get("state")
    memory = facts.get("memory") or {}
    return _j(
        {
            "state": state,
            "memory": {
                k: memory.get(k)
                for k in ("used_mb", "limit_mb", "available_mb", "pressure_pct")
            },
            "your_sessions": [e.to_agent() for e in owned],
            "others": others_summary(facts, {e.session_id for e in owned}),
            "advice": (
                advise(facts, owned, tools=TOOLS, idle_s=Config.ADVICE_IDLE_S)
                if state in ("warning", "critical")
                else []
            ),
        }
    )


@tool()
async def isabelle_release_idle(max_idle_s: int = 0) -> str:
    """Free prover memory this MCP is holding: closes every warm scratch
    session that is idle (always), and with max_idle_s > 0 also releases
    open files whose session has not been used for that many seconds (their
    next query reloads the file — slower, so opt-in). The answer to a MEMORY
    REMINDER or memory_pressure advice; then retry the failed call."""
    scratch = await pool.retire_idle_scratch(0.0)
    files = (
        await pool.release_idle_bindings(float(max_idle_s)) if max_idle_s > 0 else []
    )
    return _j({"scratch_closed": scratch, "files_released": files})
