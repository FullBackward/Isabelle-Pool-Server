"""Position-based (jEdit/LSP-style) read-only queries on the current
document: command at line, goals at line, hover, definition, and the
overlay-driven sledgehammer at a line."""
import asyncio
import time

from fastapi import APIRouter, Query

from server.app.core.logging import get_logger

from ..deps import LeasedSession, SessionManagerDep, sledgehammer_slot
from ..schemas.API_models import (
    CommandAtLineResponse,
    DefinitionResponse,
    DefinitionTarget,
    GoalsResponse,
    HoverResponse,
    SledgehammerAtRequest,
    SledgehammerAtResponse,
)
from ..serializers import parse_command_range, parse_located_command, to_ascii

router = APIRouter(tags=["positional"])
logger = get_logger(__name__)


@router.get("/api/v1/sessions/{session_id}/command_at_line", response_model=CommandAtLineResponse)
async def command_at_line(session: LeasedSession, line: int = Query(..., ge=1)):
    """Read-only jEdit-style query: the command containing `line` (1-based) of the
    current node. Snapshot-based — the proof script, rollback chain, and command
    history are untouched, and it works past a trailing theory `end`."""
    result = await asyncio.to_thread(session.command_at_line, line)
    return CommandAtLineResponse(
        found=bool(result.get("found", False)),
        kind=result.get("kind"),
        source=result.get("source"),
        range=parse_command_range(result.get("range")),
        error=result.get("error"),
    )


@router.get("/api/v1/sessions/{session_id}/goals", response_model=GoalsResponse)
async def goals_at_line(session: LeasedSession, line: int = Query(..., ge=1)):
    """Read-only jEdit-style query: rendered goal state before/after the command
    containing `line` (1-based). Snapshot-based like command_at_line. Requires
    show_states on (ISABELLE_SHOW_STATES, default true); goal lists are empty
    otherwise."""
    result = await asyncio.to_thread(session.goals_at_line, line)
    return GoalsResponse(
        found=bool(result.get("found", False)),
        command=parse_located_command(result.get("command")),
        goals_before=[to_ascii(str(g)) for g in (result.get("goals_before") or [])],
        goals_after=[to_ascii(str(g)) for g in (result.get("goals_after") or [])],
        error=result.get("error"),
    )


@router.get("/api/v1/sessions/{session_id}/hover", response_model=HoverResponse)
async def hover_at(session: LeasedSession, line: int = Query(..., ge=1), col: int = Query(..., ge=1)):
    """Hover info at a 1-based line/col (UTF-16 columns). Snapshot + Rendering —
    no evaluation, no edits; works at any document position."""
    result = await asyncio.to_thread(session.hover_at, line, col)
    return HoverResponse(
        found=bool(result.get("found", False)),
        range=parse_command_range(result.get("range")),
        contents=[to_ascii(str(c)) for c in (result.get("contents") or [])],
        error=result.get("error"),
    )


@router.get("/api/v1/sessions/{session_id}/definition", response_model=DefinitionResponse)
async def definition_at(session: LeasedSession, line: int = Query(..., ge=1), col: int = Query(..., ge=1)):
    """Go-to-definition at a 1-based line/col. Heap/source entities resolve to
    file positions; entry-document entities to in-node line ranges."""
    result = await asyncio.to_thread(session.definition_at, line, col)
    targets = [
        DefinitionTarget(**{k: v for k, v in t.items() if k in DefinitionTarget.model_fields})
        for t in (result.get("targets") or [])
        if isinstance(t, dict)
    ]
    return DefinitionResponse(
        found=bool(result.get("found", False)),
        targets=targets,
        error=result.get("error"),
    )


@router.post("/api/v1/sessions/{session_id}/sledgehammer_at", response_model=SledgehammerAtResponse)
async def sledgehammer_at(
    request: SledgehammerAtRequest,
    session: LeasedSession,
    session_manager: SessionManagerDep,
):
    """Position-explicit sledgehammer (overlay print op; no text edits). Shares
    the server-wide sledgehammer semaphore with the tip-based endpoint."""
    start = time.time()
    async with sledgehammer_slot(session_manager):
        result = await asyncio.to_thread(
            session.sledgehammer_at, request.line, request.subgoal, request.timeout_s)
    return SledgehammerAtResponse(
        found=bool(result.get("found", False)),
        results=[to_ascii(str(r)) for r in (result.get("results") or [])],
        error=result.get("error"),
        execution_time=time.time() - start,
    )
