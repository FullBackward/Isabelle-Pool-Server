"""Proof automation on the current goal: tip sledgehammer (with the
server-wide concurrency semaphore and its metrics)."""
import asyncio
import time

from fastapi import APIRouter

from server.app.core import metrics
from server.app.core.logging import get_logger
from server.app.services.success_checker import SH_PROVED, classify_sledgehammer

from ..deps import LeasedSession, SessionManagerDep, sledgehammer_slot
from ..memory_hooks import PROVER_DIED_TEXT, run_with_memory_hooks
from ..schemas.API_models import SledgehammerRequest, SledgehammerResponse
from ..serializers import to_ascii

router = APIRouter(tags=["automation"])
logger = get_logger(__name__)


@router.post("/api/v1/sessions/{session_id}/sledgehammer", response_model=SledgehammerResponse)
async def sledgehammer(
    request: SledgehammerRequest,
    session: LeasedSession,
    session_manager: SessionManagerDep,
):
    logger.info("sledgehammer requested timeout_s=%s", request.timeout_s)
    start = time.time()
    try:
        async with sledgehammer_slot(session_manager):
            suggestions, override = await asyncio.to_thread(
                run_with_memory_hooks,
                lambda: list(session.sledgehammer(request.timeout_s)),
                session,
                session_manager,
            )
    except Exception:
        metrics.sledgehammer_total.labels("failure").inc()
        raise
    finally:
        metrics.sledgehammer_seconds.observe(time.time() - start)
    elapsed = time.time() - start
    if (
        override or suggestions is None
    ):  # dead prover: nothing trustworthy; session discarded
        suggestions = []
    # success means a proof was found ("Try this: ..."), not merely that
    # sledgehammer printed something: "No proof found" used to count as success.
    outcome, method = classify_sledgehammer(suggestions)
    found = outcome == SH_PROVED
    metrics.sledgehammer_total.labels("success" if found else "failure").inc()
    logger.info(
        "sledgehammer finished found=%s suggestions=%s elapsed=%.2f",
        found, len(suggestions), elapsed,
    )
    return SledgehammerResponse(
        success=found,
        suggestions=[to_ascii(str(s)) for s in suggestions],
        raw_output=(
            PROVER_DIED_TEXT
            if override
            else "\n".join(to_ascii(str(s)) for s in suggestions)
        ),
        execution_time=elapsed,
        outcome=outcome,
        method=method,
    )
