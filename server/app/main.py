from __future__ import annotations

import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

from prometheus_fastapi_instrumentator import Instrumentator

from server.app.api.v1.router import router as api_router
from server.app.core import metrics
from server.app.core.config import API, Logging, Server
from server.app.core.logging import get_logger, reset_logging_context, set_logging_context, setup_logging
from server.app.errors import GatewayUnavailable, PoolExhausted, SessionBusyError, SessionError, SessionLeaseError, SessionNotFound, SessionStartError
from server.app.services.session_manager import SessionManager
from server.app.services.heap_pool import HeapPool, HeapPoolError

setup_logging()
logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    with_startup = time.time()
    logger.info("application startup initiated")
    sm = SessionManager()  # uses Server.IDLE_TIMEOUT_SECONDS, DEFAULT_POOL_SIZE, INITIAL_SESSIONS from config
    try:
        await sm.startup()
        sm.start_cleanup_task()
        app.state.session_manager = sm
        # Heap pool (Stage 3): registry rebuilt from persisted manifests.
        app.state.heap_pool = HeapPool()
        # Expose pool/memory/gateway gauges on /metrics (reads sm.get_lru_info()).
        metrics.register_pool_collector(sm.get_lru_info)
        metrics.register_heap_collector(app.state.heap_pool.list)
        logger.info(
            "application startup completed",
            extra={"startup_seconds": round(time.time() - with_startup, 3)},
        )
        yield
    finally:
        shutdown_start = time.time()
        logger.info("application shutdown initiated")
        try:
            await sm.shutdown()
        finally:
            logger.info(
                "application shutdown completed",
                extra={"shutdown_seconds": round(time.time() - shutdown_start, 3)},
            )


app = FastAPI(
    title="Isabelle Pool Server",
    description="RESTful API for Isabelle theorem proving",
    version=API.VERSION,
    lifespan=lifespan,
)

_STATIC_DIR = Path(__file__).parent / "static"


@app.get("/admin", include_in_schema=False)
async def admin_console():
    """Heap-pool admin console (static page; same no-auth model as the API —
    keep behind the firewall/SSH tunnel like everything else).

    When ISABELLE_ADMIN_TOKEN is configured it is injected into the page so
    the console's force-close can call the token-gated admin listing; without
    a token the close buttons disable themselves client-side."""
    html = (_STATIC_DIR / "admin.html").read_text(encoding="utf-8")
    html = html.replace("__ADMIN_TOKEN__", Server.ADMIN_TOKEN)
    return HTMLResponse(html)


@app.middleware("http")
async def request_logging_middleware(request: Request, call_next):
    request_id = request.headers.get(Logging.REQUEST_HEADER_NAME) or uuid.uuid4().hex[:12]
    token = set_logging_context(request_id=request_id)
    request.state.request_id = request_id
    start = time.time()

    logger.info("request started %s %s", request.method, request.url.path)

    try:
        response = await call_next(request)
    except Exception:
        duration_ms = round((time.time() - start) * 1000, 2)
        logger.exception(
            "request failed %s %s in %sms",
            request.method,
            request.url.path,
            duration_ms,
        )
        reset_logging_context(token)
        raise

    duration_ms = round((time.time() - start) * 1000, 2)
    response.headers[Logging.REQUEST_HEADER_NAME] = request_id
    logger.info(
        "request completed %s %s status=%s duration_ms=%s",
        request.method,
        request.url.path,
        response.status_code,
        duration_ms,
    )
    reset_logging_context(token)
    return response


@app.exception_handler(SessionNotFound)
async def handle_session_not_found(request: Request, exc: SessionNotFound):
    logger.warning("session not found on %s: %s", request.url.path, exc)
    return JSONResponse(status_code=404, content={"detail": str(exc) or "Session not found"})


@app.exception_handler(GatewayUnavailable)
async def handle_gateway_unavailable(request: Request, exc: GatewayUnavailable):
    logger.error("gateway unavailable on %s: %s", request.url.path, exc)
    return JSONResponse(status_code=503, content={"detail": str(exc) or "REPL gateway unavailable"})


@app.exception_handler(SessionStartError)
async def handle_session_start_error(request: Request, exc: SessionStartError):
    logger.error("session startup failed on %s: %s", request.url.path, exc)
    return JSONResponse(status_code=500, content={"detail": str(exc) or "Session start failed"})


@app.exception_handler(SessionLeaseError)
async def handle_session_lease_error(request: Request, exc: SessionLeaseError):
    logger.warning("session lease error on %s: %s", request.url.path, exc)
    return JSONResponse(status_code=403, content={"detail": str(exc) or "Invalid lease token"})


@app.exception_handler(SessionBusyError)
async def handle_session_busy_error(request: Request, exc: SessionBusyError):
    logger.warning("session busy on %s: %s", request.url.path, exc)
    return JSONResponse(status_code=409, content={"detail": str(exc) or "Session is busy"})


@app.exception_handler(PoolExhausted)
async def handle_pool_exhausted(request: Request, exc: PoolExhausted):
    logger.warning("pool exhausted on %s: %s", request.url.path, exc)
    return JSONResponse(
        status_code=503,
        content={"detail": str(exc) or "Session pool exhausted"},
        headers={"Retry-After": "5"},
    )


@app.exception_handler(SessionError)
async def handle_session_error(request: Request, exc: SessionError):
    """Base handler for session-layer failures (backend errors/timeouts).

    Subclasses keep their specific handlers above (FastAPI resolves by MRO);
    this catches plain SessionError so the constructed message (e.g.
    'TimeoutError: Backend call timed out after 30.0s') reaches the client
    instead of collapsing into a generic 'Internal server error'.
    """
    logger.error("session error on %s: %s", request.url.path, exc)
    return JSONResponse(status_code=500, content={"detail": str(exc) or "Session error"})


@app.exception_handler(HeapPoolError)
async def handle_heap_pool_error(request: Request, exc: HeapPoolError):
    logger.warning("heap pool error on %s: %s", request.url.path, exc)
    return JSONResponse(
        status_code=getattr(exc, "status_code", 500),
        content={"detail": str(exc) or "Heap pool error"},
    )


@app.exception_handler(Exception)
async def handle_uncaught_exception(request: Request, exc: Exception):
    logger.exception("uncaught exception on %s", request.url.path)
    return JSONResponse(status_code=500, content={"detail": "Internal server error"})


app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(api_router)

# HTTP request metrics (count + latency histograms, grouped by route template)
# and the /metrics endpoint serving the default registry — which also includes
# the isabelle_pool_server_* counters/gauges defined in server.app.core.metrics.
Instrumentator().instrument(app).expose(app, endpoint="/metrics", include_in_schema=False)


if __name__ == "__main__":
    import uvicorn

    logger.info("starting Isabelle Pool Server on %s:%s", Server.HOST, Server.PORT)
    uvicorn.run(
        app,
        host=Server.HOST,
        port=Server.PORT,
        log_level=Logging.LOG_LEVEL.lower(),
        log_config=None,
        access_log=False,
    )
