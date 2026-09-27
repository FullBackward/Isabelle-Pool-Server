"""Execution: small-step commands, verify_chunk, whole-document load, theory
entry, the lease-free big-step build, and the stateless header parser."""
import asyncio
from typing import Optional

from fastapi import APIRouter, HTTPException

from server.app.core.config import Logging
from server.app.core.logging import get_logger, logging_context
from server.app.services.theory_parsing import parse_theory_header, suggested_field

from ..deps import LeasedSession, SessionManagerDep
from ..schemas.API_models import (
    BigStepTheoryRequest,
    ChunkVerifyRequest,
    ChunkVerifyResponse,
    CommandMessage,
    CommandRequest,
    CommandResponse,
    CommandStatus,
    DocumentLoadRequest,
    DocumentLoadResponse,
    EnterTheoryRequest,
    ParseTheoryHeaderRequest,
    ParseTheoryHeaderResponse,
)
from ..serializers import parse_command_range, preview

router = APIRouter(tags=["execution"])
logger = get_logger(__name__)


@router.post("/api/v1/sessions/{session_id}/commands", response_model=CommandResponse)
async def execute_command(request: CommandRequest, session: LeasedSession):
    logger.info(
        "executing command timeout=%s preview=%s",
        request.timeout,
        preview(request.command, Logging.COMMAND_PREVIEW_CHARS),
    )
    result = await asyncio.to_thread(session.execute_command, request.command, request.timeout)
    logger.info(
        "command finished success=%s execution_time=%s",
        getattr(result, "success", False),
        float(getattr(result, "execution_time", 0.0) or 0.0),
    )
    return CommandResponse(
        success=getattr(result, "success", False),
        output=getattr(result, "output", None),
        error=getattr(result, "error", None),
        subgoal_error=getattr(result, "subgoal_error", None),
        subgoals=getattr(result, "subgoals", []) or [],
        execution_time=float(getattr(result, "execution_time", 0.0) or 0.0),
    )


@router.post("/api/v1/sessions/{session_id}/verify_chunk", response_model=ChunkVerifyResponse)
async def verify_chunk(request: ChunkVerifyRequest, session: LeasedSession):
    logger.info(
        "verify_chunk timeout=%s preview=%s",
        request.timeout,
        preview(request.chunk, Logging.COMMAND_PREVIEW_CHARS),
    )
    result = await asyncio.to_thread(session.verify_chunk, request.chunk, request.timeout)
    report = result.get("report", {}) or {}
    commands = [
        CommandStatus(
            index=int(c.get("i", 0)),
            line=int(c.get("line", 0)),
            node_line=c.get("node_line"),
            kind=str(c.get("kind", "")),
            status=str(c.get("status", "unprocessed")),
            range=parse_command_range(c.get("range")),
            messages=[CommandMessage(sev=str(m.get("sev", "")), text=str(m.get("text", "")))
                      for m in (c.get("messages", []) or [])],
        )
        for c in (report.get("commands", []) or [])
    ]
    timed_out = bool(report.get("timed_out", False))
    proof_open = bool(report.get("proof_open", False))
    pending_qed = bool(report.get("pending_qed", False))
    used_sorry = bool(report.get("used_sorry", False))
    stuck_line = next((c.line for c in commands if c.status == "running"), None)
    success = (not timed_out) and len(commands) > 0 and all(c.status == "ok" for c in commands)
    logger.info(
        "verify_chunk done success=%s proof_open=%s pending_qed=%s used_sorry=%s timed_out=%s commands=%s stuck_line=%s",
        success, proof_open, pending_qed, used_sorry, timed_out, len(commands), stuck_line,
    )
    return ChunkVerifyResponse(
        success=success,
        proof_open=proof_open,
        pending_qed=pending_qed,
        used_sorry=used_sorry,
        timed_out=timed_out,
        stuck_line=stuck_line,
        commands=commands,
        execution_time=float(result.get("execution_time", 0.0) or 0.0),
        error=report.get("error"),
    )


@router.put("/api/v1/sessions/{session_id}/document", response_model=DocumentLoadResponse)
async def load_document(request: DocumentLoadRequest, session: LeasedSession):
    """Replace the session's whole document with ``text`` (the file-sync primitive
    for read-only, file-mirroring clients). Resets the backend document and all
    session bookkeeping, then re-enters the theory and issues the text as one
    edit. See DocumentLoadRequest for the two header modes."""
    logger.info(
        "load_document requested thy_name=%s imports=%s report=%s preview=%s",
        request.thy_name,
        request.imports,
        request.report,
        preview(request.text, Logging.COMMAND_PREVIEW_CHARS),
    )
    result = await asyncio.to_thread(
        session.load_document, request.text, request.thy_name, request.imports, request.timeout, request.report
    )
    logger.info(
        "load_document finished success=%s execution_time=%s",
        getattr(result, "success", False),
        float(getattr(result, "execution_time", 0.0) or 0.0),
    )
    return DocumentLoadResponse(
        success=getattr(result, "success", False),
        theory=session.entered_thy,
        output=getattr(result, "output", None),
        error=getattr(result, "error", None),
        execution_time=float(getattr(result, "execution_time", 0.0) or 0.0),
        report=(session.last_chunk_report or {}).get("report") if request.report else None,
    )


@router.get("/api/v1/sessions/{session_id}/last_report")
async def get_last_report(session: LeasedSession):
    """The retained report of the session's MOST RECENT verify_chunk call
    (success or failure). 404 until the first verify_chunk. Cleared by
    load_document; NOT cleared by rollback/restore."""
    if session.last_chunk_report is None:
        raise HTTPException(status_code=404, detail="no verify_chunk report yet for this session")
    logger.debug("returning last verify_chunk report")
    return session.last_chunk_report


@router.post("/api/v1/sessions/{session_id}/enter_theory/{theory_name}")
async def enter_theory(
    theory_name: str,
    session: LeasedSession,
    request: Optional[EnterTheoryRequest] = None,
):
    imports = request.imports if request else None
    logger.info("entering theory theory_name=%s imports=%s", theory_name, imports)
    await asyncio.to_thread(lambda: session.enter_thy(theory_name, imports=imports))
    return {"success": True, "message": f"Entered theory {theory_name}", "imports": imports}


@router.post("/api/v1/sessions/bigstep", response_model=CommandResponse)
async def execute_big_step(request: BigStepTheoryRequest, session_manager: SessionManagerDep):
    result = await session_manager.verify_big_step_build(
        theory_name=request.theory_name,
        theory=request.theory,
        dependencies=request.dependencies,
        field=request.field,
        timeout=request.timeout,
    )
    return CommandResponse(
        success=result.success,
        output=result.output,
        error=result.error,
        subgoals=result.subgoals,
        execution_time=result.execution_time,
        mode=result.mode,
        theory_verified=result.theory_verified,
    )


@router.post("/api/v1/parse_theory_header", response_model=ParseTheoryHeaderResponse)
async def parse_theory_header_endpoint(request: ParseTheoryHeaderRequest):
    """Stateless canonical theory-header parse (no session, no lease).

    The one parser every consumer should use: comments (nested) are stripped
    first, then the `theory <name> imports <...> begin` header is anchored —
    so a leading `(* TASK: ... *)`-style comment can never pollute the import
    list (isabellegym-header-imports-issue.md)."""
    with logging_context():
        name, imports = parse_theory_header(request.text)
        return ParseTheoryHeaderResponse(
            theory_name=name,
            imports=imports,
            suggested_field=suggested_field(imports),
        )
