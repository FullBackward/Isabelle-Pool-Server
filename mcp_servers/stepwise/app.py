"""Isabelle Pool Server MCP server: tools / resources / prompts over PoolAsyncClient.

Tools wrap the (now-complete) async client only — no direct HTTP. Sessions/leases are
auto-managed and isolated per MCP connection (see pool.py). Parallelism is exposed at two
levels: intra-proof via `verify_chunk` (parallel_proofs=2) and inter-session via
`verify_batch` (concurrent fan-out across the pool in one call).
"""
from __future__ import annotations

import json
from typing import Any, Awaitable, Callable, Dict, List, Optional, TypeVar

from mcp.server.fastmcp import Context, FastMCP

from client.async_client import PoolAsyncClient

from ..common import advise, make_guard, others_summary
from .config import TOOLS, Config
from .pool import SessionPool

mcp = FastMCP("isabelle-pool-server", host=Config.HOST, port=Config.PORT)
pool = SessionPool()

# Memory protocol (plan §2.C, owner decisions D3/D4): the server reports FACTS
# (503 bodies with an `error` key, the X-Memory-State header, /api/v1/memory);
# this server words the advice and names its own tools. Every tool is wrapped:
# an HTTP error becomes structured JSON with advice, and while the server is at
# warning/critical a MEMORY REMINDER is attached to results.
_F = TypeVar("_F", bound=Callable[..., Awaitable[Any]])


def _owner_of(args: tuple, kwargs: Dict[str, Any]) -> Any:
    ctx = kwargs.get("ctx")
    if ctx is None:
        ctx = next((a for a in args if isinstance(a, Context)), None)
    return pool.conn_key(ctx) if ctx is not None else pool.conn_key(None)


_guard = make_guard(
    registry=pool.registry,
    tools=TOOLS,
    get_client=pool.client,
    owner_of=_owner_of,
    idle_s=Config.ADVICE_IDLE_S,
)


def tool(**kw: Any) -> Callable[[_F], _F]:
    """`@mcp.tool()` + the memory guard (see TOOLS)."""

    def deco(fn: _F) -> _F:
        return mcp.tool(**kw)(_guard(fn))  # type: ignore[no-any-return]

    return deco


def _render_chunk(report: Dict[str, Any], detail: bool) -> str:
    """Terse-by-default: summary + only failed/running rows. detail=True → full table."""
    if detail:
        return PoolAsyncClient.format_chunk_report(report)
    cmds = report.get("commands", []) or []
    proof_open = report.get("proof_open")
    pending_qed = report.get("pending_qed")
    used_sorry = report.get("used_sorry")
    head = (
        f"success={report.get('success')} proof_open={proof_open} pending_qed={pending_qed} "
        f"used_sorry={used_sorry} "
        f"timed_out={report.get('timed_out')} stuck_line={report.get('stuck_line')} "
        f"time={float(report.get('execution_time', 0) or 0):.2f}s commands={len(cmds)}"
    )
    bad = [c for c in cmds if c.get("status") in ("failed", "running")]
    lines = [head]
    if report.get("error"):
        lines.append(f"  ERROR: {report['error']}")
    for c in bad:
        msgs = "; ".join((m.get("text", "") or "")[:200] for m in (c.get("messages") or []))
        lines.append(f"  line {c.get('line')} {c.get('kind')} {c.get('status')} {msgs}".rstrip())
    if cmds and not bad:
        lines.append("  (all commands ok)")
    if report.get("success") and pending_qed:
        # Goal discharged but the proof block still awaits its closing `qed` —
        # batch builds reject this state with "Goal present in this block".
        lines.append("  NOTE: goal discharged but the proof block is NOT closed — "
                     "submit a bare `qed` chunk to finish (do NOT start new proof work).")
    elif report.get("success") and proof_open:
        # No command errored, but the proof is still OPEN (e.g. `using assms`/trailing `have`
        # with no `qed`): the theorem is NOT proved. Warn so the agent closes/rolls back the
        # goal before declaring a new theorem (else: "Bad context for command ...").
        lines.append("  NOTE: proof is still OPEN (theorem NOT proved). Close it (qed / "
                     "terminal method) or rollback before a new theorem/lemma.")
    if used_sorry:
        lines.append("  NOTE: chunk uses sorry/oops — the theorem is NOT actually proved.")
    lines.append("  [call with detail=True for the full per-command table]")
    return "\n".join(lines)


# --------------------------------------------------------------------------- tools


@tool()
async def enter_theory(
    name: str, ctx: Context, imports: Optional[List[str]] = None, field: str = "HOL",
) -> str:
    """Start (or restart) a proof session for this connection in theory `name`.

    Acquires a leased session, enters the theory, and processes its `begin` header so that
    `verify_chunk`/`step` work immediately. Closes any prior theory on this connection.


    MEMORY: a result with error=memory_pressure (or a memory_reminder field) means the
    prover server is short of memory — follow its advice: close_theory() frees this
    connection's session, memory_status() shows who holds the memory; if the advice
    says to, stop and tell the user.
    """
    imps = imports or ["Main"]
    cur = await pool.open_theory(ctx, name, imps, field)
    return f"entered theory '{name}' (imports {imps}, field {field}); session bound to this connection"


@tool()
async def verify_chunk(text: str, ctx: Context, timeout: float = Config.CHUNK_TIMEOUT, detail: bool = False) -> str:
    """Submit Isar text — one command or a whole proof — and verify it in one call under a
    SINGLE wall budget. This is the only execution tool (it subsumes single-stepping).

    Returns per-command status in source order (ok/failed/running/unprocessed). On timeout
    the report is partial and names the still-`running` line (the likely loop). A `running`
    line at timeout means a LOOPING method (e.g. blast/auto spinning on the wrong goal) —
    replace that method or call sledgehammer on the goal; never resubmit a near-identical
    command, and pass a smaller `timeout` for exploratory chunks. Terse by
    default; pass detail=True for the full per-command table. (For the resulting goal/
    subgoals, call proof_state.)

    success=True means NO command errored — NOT that the theorem is proved. The theorem is
    proved ONLY when success=True AND proof_open=False AND used_sorry=False:
      - proof_open=True  -> the chunk left the proof block unclosed. Two cases:
          * subgoals remain (pending_qed=False): inspect them with proof_state and keep
            proving. Do NOT declare a new theorem/lemma while proof_open=True
            (it triggers "Bad context for command ... -- using reset state") — close the
            goal or rollback first.
          * the goal is discharged but `qed` is missing (pending_qed=True): submit a bare
            `qed` chunk to finish — batch verification rejects this state with
            "Goal present in this block". Do NOT start new proof work.
      - used_sorry=True  -> the chunk used `sorry`/`oops`; the theorem is NOT proved. Never use
        sorry/oops to "pass".


    MEMORY: a result with error=memory_pressure (or a memory_reminder field) means the
    prover server is short of memory — follow its advice: close_theory() frees this
    connection's session, memory_status() shows who holds the memory; if the advice
    says to, stop and tell the user.
    """
    c = await pool.client()
    cur = pool.require_current(ctx)
    report = await c.verify_chunk(cur.session_id, text, timeout=timeout, lease_id=cur.lease_id)
    return _render_chunk(report, detail)


@tool()
async def proof_state(ctx: Context) -> str:
    """Current goal / open subgoals. The right call after verify_chunk reports
    proof_open=True — inspect the remaining goal before deciding how to close it
    (or before sledgehammer)."""
    c = await pool.client()
    cur = pool.require_current(ctx)
    return json.dumps(await c.get_proof_state(cur.session_id, lease_id=cur.lease_id), default=str)


@tool()
async def source(ctx: Context) -> str:
    """Current theory source as the prover sees it."""
    c = await pool.client()
    cur = pool.require_current(ctx)
    return json.dumps(await c.get_source(cur.session_id, lease_id=cur.lease_id), default=str)


@tool()
async def diagnostic(command: str, ctx: Context) -> str:
    """Run a READ-ONLY Isabelle diagnostic/query command and return its output.

    Use this to INSPECT state without changing the proof — it is the right tool when
    `verify_chunk` would silently discard the output of a query:
      - `thm <name>`              show a theorem's statement (e.g. `thm conjI`)
      - `term "..."` / `prop "..."` / `typ "..."`   parse and print a term/prop/type
      - `find_theorems "<pat>"`   search for matching theorems (e.g. `find_theorems "_ + 0 = _"`)
      - `find_consts "<pat>"`     search for matching constants
      - `prf <name>` / `full_prf <name>`            print proof terms
      - `print_theorems` / `print_facts` / `print_statement <name>` / any `print_*`

    The command runs transiently (the proof script and rollback chain are untouched).
    Code-executing / IO commands (ML, setup, *_file, ...) are rejected. Requires a theory
    to be entered first (`enter_theory`). Returns {success, output, error, execution_time}.
    """
    c = await pool.client()
    cur = pool.require_current(ctx)
    return json.dumps(await c.diagnostic(cur.session_id, command, lease_id=cur.lease_id), default=str)


@tool()
async def sledgehammer(ctx: Context, timeout_s: int = 30) -> str:
    """Run Isabelle's sledgehammer on the current open goal; returns proof-method
    suggestions.

    Requires an OPEN goal (call after verify_chunk reports proof_open=True).
    Paste the returned suggestion verbatim into your next verify_chunk — do not
    paraphrase it. timeout_s (default 30) bounds the search; on timeout retry
    once on a smaller sub-goal rather than inventing your own solver call."""
    c = await pool.client()
    cur = pool.require_current(ctx)
    return json.dumps(await c.sledgehammer(cur.session_id, timeout_s=timeout_s, lease_id=cur.lease_id), default=str)


@tool()
async def checkpoint(ctx: Context) -> str:
    """Save a checkpoint of the current proof state; returns {checkpoint_id, timestamp}."""
    c = await pool.client()
    cur = pool.require_current(ctx)
    return json.dumps(await c.save_checkpoint(cur.session_id, lease_id=cur.lease_id), default=str)


@tool()
async def restore(checkpoint_id: int, ctx: Context) -> str:
    """Restore a previously saved checkpoint."""
    c = await pool.client()
    cur = pool.require_current(ctx)
    return json.dumps(await c.restore_checkpoint(cur.session_id, checkpoint_id, lease_id=cur.lease_id), default=str)


@tool()
async def rollback(ctx: Context) -> str:
    """Roll back the most recent command/edit in the current theory.

    NOTE: a FAILED verify_chunk already rolls its failed commands back
    automatically — do NOT call rollback after a failure (you would undo a
    good command). rollback is only for retracting a SUCCESSFUL chunk you no
    longer want."""
    c = await pool.client()
    cur = pool.require_current(ctx)
    try:
        return json.dumps(await c.rollback(cur.session_id, lease_id=cur.lease_id), default=str)
    except Exception as e:
        return json.dumps({"success": False, "error": str(e)})


@tool()
async def close_theory(ctx: Context) -> str:
    """Close this connection's current session (the server-side backend is
    destroyed, not pooled for reuse). Call when done with a theorem — and when
    a MEMORY REMINDER or memory_pressure advice asks you to free memory."""
    closed = await pool.close_for(ctx)
    return "closed" if closed else "no active session"


@tool()
async def verify_batch(
    items: List[Dict[str, Any]], ctx: Context,
    max_parallel: int = Config.MAX_PARALLEL, timeout: float = Config.CHUNK_TIMEOUT,
) -> str:
    """Verify MANY independent proof chunks CONCURRENTLY in one call.

    Each item = {"name": str, "imports": [str]?, "field": str?, "chunk": str}. Fans out
    across the server's session pool (bounded by max_parallel) — this is how a sequential
    agent exploits the server's inter-session parallelism. Returns a per-item summary.
    """
    results = await pool.verify_batch(
        items, max_parallel, timeout, owner=pool.conn_key(ctx)
    )
    return json.dumps(results, default=str, indent=1)


@tool()
async def list_my_sessions(ctx: Context) -> str:
    """The prover sessions THIS connection holds (its theory, and verify_batch
    items while they run), with idle time, when the server will reap them,
    and the memory attributed to each (refreshed from the server).

    `ref` is display-only: no tool takes it — act by theory name
    (enter_theory / close_theory). Use with memory_status() when a result
    reports memory_pressure or carries a memory_reminder."""
    c = await pool.client()
    owner = pool.conn_key(ctx)
    try:
        facts = await c.memory_status()
        pool.registry.merge(facts.get("sessions") or [])
        pool.registry.mark_missing(facts.get("sessions") or [])
    except Exception:  # pylint: disable=broad-exception-caught  # local view still useful
        pass
    return json.dumps(
        {
            "sessions": [e.to_agent() for e in pool.registry.owned(owner)],
            "memory_state": c.last_memory_state,
        },
        default=str,
    )


@tool()
async def memory_status(ctx: Context) -> str:
    """Prover-server memory: state (ok / warning / critical), used vs limit,
    what this connection holds, what others hold (sessions, task groups,
    builds, external pressure), and advice when memory is tight. Call it
    after a memory_pressure error or a MEMORY REMINDER; if the advice says
    the memory is held by others, stop and tell the user."""
    c = await pool.client()
    owner = pool.conn_key(ctx)
    facts = await c.memory_status()
    pool.registry.merge(facts.get("sessions") or [])
    owned = pool.registry.owned(owner)
    state = facts.get("state")
    memory = facts.get("memory") or {}
    return json.dumps(
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
        },
        default=str,
    )


# ----------------------------------------------------------------------- resources

@mcp.resource("isabelle-pool-server://health")
async def health() -> str:
    """Server health: gateway_alive, pool size, memory pressure."""
    c = await pool.client()
    return json.dumps(await c.health(), default=str, indent=1)


@mcp.resource("isabelle-pool-server://sessions")
async def sessions() -> str:
    """Active sessions in the server pool."""
    c = await pool.client()
    return json.dumps(await c.list_sessions(), default=str, indent=1)


# ------------------------------------------------------------------------- prompts

@mcp.prompt()
def prove_theorem(theorem: str, imports: str = "Main") -> str:
    """Minimal prompt listing all available MCP tools."""
    return (
        f"Prove the following Isabelle/HOL theorem.\n\n"
        f"```isabelle\n{theorem}\n```\n\n"
        f"AVAILABLE TOOLS:\n"
        f"- enter_theory(name, imports=[...]) — start a proof session\n"
        f"- verify_chunk(text) — submit proof commands and check status\n"
        f"- proof_state() — inspect current open subgoals\n"
        f"- source() — view current theory source text\n"
        f"- sledgehammer() — run automated proof search on open goal\n"
        f"- diagnostic(command) — run read-only queries (thm, term, find_theorems, ...)\n"
        f"- checkpoint() — save current proof state\n"
        f"- restore(checkpoint_id) — restore a saved checkpoint\n"
        f"- rollback() — undo the most recent edit\n"
        f"- close_theory() — release the proof session\n"
        f"- verify_batch(items=[...]) — verify multiple independent proof chunks in parallel\n"
        f"- list_my_sessions() / memory_status() — what you hold on the prover server and its memory\n"
        f"\n"
        f"MEMORY: if a result reports error=memory_pressure or carries a memory_reminder, follow its\n"
        f"advice (close_theory() frees your session; if it says the memory is held by others, stop\n"
        f"and tell the user).\n"
        f"PROVED WHEN: verify_chunk reports success=True, proof_open=False, used_sorry=False.\n"
        f"Reply DONE when the theorem is proved.\n"
    )


def main() -> None:
    transport = Config.TRANSPORT
    if transport == "streamable-http":
        mcp.run(transport="streamable-http")
    else:
        mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
