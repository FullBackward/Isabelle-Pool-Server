"""Proof automation on the current goal: tip sledgehammer (with the
server-wide concurrency semaphore and its metrics)."""
import asyncio
import time

from fastapi import APIRouter

from server.app.core import metrics
from server.app.core.logging import get_logger

from ..deps import LeasedSession, SessionManagerDep, sledgehammer_slot
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
            suggestions: list = await asyncio.to_thread(session.sledgehammer, request.timeout_s)
    except Exception:
        metrics.sledgehammer_total.labels("failure").inc()
        raise
    finally:
        metrics.sledgehammer_seconds.observe(time.time() - start)
    elapsed = time.time() - start
    found = len(suggestions) > 0
    metrics.sledgehammer_total.labels("success" if found else "failure").inc()
    logger.info(
        "sledgehammer finished found=%s suggestions=%s elapsed=%.2f",
        found, len(suggestions), elapsed,
    )
    return SledgehammerResponse(
        success=found,
        suggestions=[to_ascii(str(s)) for s in suggestions],
        raw_output="\n".join(to_ascii(str(s)) for s in suggestions),
        execution_time=elapsed,
    )
