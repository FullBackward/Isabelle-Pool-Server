"""State management: checkpoints, restore, rollback."""
import asyncio

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from server.app.core.logging import get_logger

from ..deps import LeasedSession
from ..schemas.API_models import StateCheckpoint

router = APIRouter(tags=["checkpoints"])
logger = get_logger(__name__)


@router.post("/api/v1/sessions/{session_id}/checkpoints", response_model=StateCheckpoint)
async def save_checkpoint(session: LeasedSession):
    logger.info("saving checkpoint")
    cp = await asyncio.to_thread(session.save_checkpoint)
    logger.info("checkpoint saved checkpoint_id=%s", getattr(cp, "checkpoint_id", None))
    return StateCheckpoint(
        checkpoint_id=int(getattr(cp, "checkpoint_id")),
        timestamp=float(getattr(cp, "timestamp")),
    )


@router.post("/api/v1/sessions/{session_id}/checkpoints/{checkpoint_id}/restore")
async def restore_checkpoint(checkpoint_id: int, session: LeasedSession):
    logger.info("restoring checkpoint checkpoint_id=%s", checkpoint_id)
    success = await asyncio.to_thread(session.restore_checkpoint, checkpoint_id)
    ok = bool(success) if isinstance(success, bool) else False
    logger.info("checkpoint restore finished success=%s checkpoint_id=%s", ok, checkpoint_id)
    return {
        "success": ok,
        "checkpoint_id": checkpoint_id,
        "message": "State restored successfully" if ok else "Restoration failed",
    }


@router.post("/api/v1/sessions/{session_id}/rollback")
async def rollback(session: LeasedSession):
    logger.info("rolling back latest command")
    result = await asyncio.to_thread(session.rollback)
    output = result.total_output() if hasattr(result, "total_output") else ""
    if output and "No text edits have been made to rollback" in str(output):
        return JSONResponse(
            status_code=409,
            content={"success": False, "error": "no edits to roll back", "output": output},
        )
    return {"success": True, "output": output}
