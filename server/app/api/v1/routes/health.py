"""Health: human summary, liveness, readiness."""
import asyncio
from datetime import datetime

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from server.app.core.config import API
from server.app.core.logging import get_logger

from ..deps import SessionManagerDep

router = APIRouter(tags=["health"])
logger = get_logger(__name__)


@router.get("/")
async def root(session_manager: SessionManagerDep):
    # get_lru_info runs the (cached, up to 5 s) gateway liveness probe — a Py4J
    # round-trip — so it goes to a worker thread, never the event loop (Bug 16).
    lru = (await asyncio.to_thread(session_manager.get_lru_info)
           if hasattr(session_manager, "get_lru_info") else {})
    logger.debug("root health endpoint requested")
    gateway_alive = lru.get("gateway_alive", True)
    return {
        "service": "IsabelleGym Server",
        "version": API.VERSION,
        "status": "healthy" if gateway_alive else "degraded",
        "gateway_alive": gateway_alive,
        "active_sessions": lru.get("active_sessions", 0),
        "busy_sessions": lru.get("busy_sessions", 0),
        "max_pool_size": lru.get("max_pool_size", 0),
        "max_concurrent_sledgehammer": lru.get("max_concurrent_sledgehammer", 0),
        "memory_management_enabled": lru.get("memory_management_enabled", False),
        "memory_used_mb": lru.get("memory_used_mb", 0),
        "memory_limit_mb": lru.get("memory_limit_mb", 0),
        "memory_pressure_pct": lru.get("memory_pressure_pct", 0),
        "timestamp": datetime.now().isoformat(),
    }


@router.get("/healthz")
async def healthz():
    """Liveness probe: 200 as long as the process serves requests.

    Deliberately does NOT depend on the session manager / gateway — a live but
    not-yet-ready process should restart on readiness, not liveness.
    """
    return {"status": "alive"}


@router.get("/readyz")
async def readyz(request: Request):
    """Readiness probe: 200 only when the session manager is up and the REPL
    gateway is alive; 503 otherwise (so traffic isn't routed to a degraded
    instance)."""
    sm = getattr(request.app.state, "session_manager", None)
    # Probe off the loop: a stale cache means a Py4J round-trip of up to 5 s.
    alive = bool(sm is not None and await asyncio.to_thread(sm.gateway_alive))
    if alive:
        return {"status": "ready", "gateway_alive": True}
    return JSONResponse(
        status_code=503,
        content={"status": "not_ready", "gateway_alive": alive},
    )
