"""Read-only inspection of a session: diagnostic queries, proof state,
subgoals, facts, source. None of these mutate the proof script."""
import asyncio

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse

from server.app.core.config import Logging
from server.app.core.logging import get_logger
from server.app.services.internal_models import SessionExecutionError

from ..deps import LeasedSession
from ..schemas.API_models import (
    DiagnosticRequest,
    DiagnosticResponse,
    FactsResponse,
    ProofStateResponse,
)
from ..serializers import preview, to_ascii

router = APIRouter(tags=["inspection"])
logger = get_logger(__name__)


@router.post("/api/v1/sessions/{session_id}/diagnostic", response_model=DiagnosticResponse)
async def run_diagnostic(request: DiagnosticRequest, session: LeasedSession):
    """Run a single READ-ONLY diagnostic command (thm, term, find_theorems, print_*, ...)
    and return its output. The command runs transiently and does not alter the proof. Input
    is gatekept by the DiagnosticRequest validator (allowlist of diagnostic keywords + denylist
    of code-execution/IO commands); rejected input returns HTTP 422 before reaching here."""
    logger.info(
        "diagnostic requested preview=%s",
        preview(request.command, Logging.COMMAND_PREVIEW_CHARS),
    )
    result = await asyncio.to_thread(session.run_diagnostic, request.command, request.timeout)
    logger.info(
        "diagnostic finished success=%s execution_time=%s",
        getattr(result, "success", False),
        float(getattr(result, "execution_time", 0.0) or 0.0),
    )
    return DiagnosticResponse(
        success=getattr(result, "success", False),
        output=to_ascii(getattr(result, "output", None)),
        error=getattr(result, "error", None),
        execution_time=float(getattr(result, "execution_time", 0.0) or 0.0),
    )


@router.get("/api/v1/sessions/{session_id}/state")
async def get_proof_state(session: LeasedSession):
    logger.debug("fetching proof state")
    state = await asyncio.to_thread(session.get_proof_state)
    if isinstance(state, SessionExecutionError):
        logger.warning("proof state fetch failed: %s", state.error)
        return JSONResponse(
            status_code=500,
            content={"error": state.error, "execution_time": state.execution_time},
        )
    return ProofStateResponse(
        subgoals=[to_ascii(s) for s in (state.subgoals or [])],
        proof_finished=state.proof_finished,
        pending_qed=state.pending_qed,
        current_theory=state.current_theory,
    )


@router.get("/api/v1/sessions/{session_id}/subgoals")
async def get_subgoals(session: LeasedSession):
    state = await asyncio.to_thread(session.get_proof_state)
    if isinstance(state, SessionExecutionError):
        logger.warning("subgoals fetch failed: %s", state.error)
        return JSONResponse(
            status_code=500,
            content={"error": state.error, "execution_time": state.execution_time},
        )
    subgoals = [to_ascii(s) for s in (state.subgoals or [])]
    logger.debug("returning %s subgoals", len(subgoals))
    return {
        "subgoals": subgoals,
        "count": len(subgoals),
        "proof_finished": state.proof_finished,
    }


@router.get("/api/v1/sessions/{session_id}/facts/local", response_model=FactsResponse)
async def get_local_facts(session: LeasedSession):
    """Read-only probe: facts in the current local proof context. Transient —
    the proof script, rollback chain, and command history are untouched."""
    facts = [to_ascii(f) for f in await asyncio.to_thread(session.local_facts)]
    logger.debug("returning %s local facts", len(facts))
    return FactsResponse(facts=facts, count=len(facts))


@router.get("/api/v1/sessions/{session_id}/facts/global", response_model=FactsResponse)
async def get_global_facts(session: LeasedSession, limit: int = Query(100, ge=1, le=1000)):
    """Read-only probe: theory-level facts, sorted by name, capped at ``limit``.
    Transient — the proof script, rollback chain, and command history are untouched."""
    facts = [to_ascii(f) for f in await asyncio.to_thread(session.global_facts, limit)]
    logger.debug("returning %s global facts (limit=%s)", len(facts), limit)
    return FactsResponse(facts=facts, count=len(facts))


@router.get("/api/v1/sessions/{session_id}/source")
async def get_source(session: LeasedSession):
    logger.debug("fetching theory source")
    source_result = await asyncio.to_thread(session.get_source)
    current_thy = await asyncio.to_thread(lambda: session.current_thy)
    source_text = source_result.total_output() if hasattr(source_result, "total_output") else str(source_result)
    return {"source": source_text, "theory": current_thy}
