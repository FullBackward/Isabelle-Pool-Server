"""Shared FastAPI dependencies and request-level guards for the v1 API.

Every session-scoped endpoint used to repeat the same four lines: read the
``X-Lease-Id`` header, require it, resolve the session with ``require_lease``,
and open a ``logging_context(session_id=...)``. ``LeasedSession`` does that
once, as a dependency, so a handler declares ``session: LeasedSession`` and
gets the resolved, lease-checked session with the log context already set.

Admin gating and path-segment validation live here too so the three
destructive heap endpoints and the admin listing share one implementation.
"""
from contextlib import asynccontextmanager
from typing import Annotated, AsyncIterator, Optional

from fastapi import Depends, Header, HTTPException

from server.app.core import metrics
from server.app.core.config import Server
from server.app.core.input_guards import validate_safe_name
from server.app.core.logging import get_logger, set_logging_context
from server.app.dependencies import get_heap_pool, get_session_manager
from server.app.errors import SessionLeaseError
from server.app.services.heap_pool import HeapPool
from server.app.services.session import _Isabelle_Session
from server.app.services.session_manager import SessionManager

logger = get_logger(__name__)

# Header parameters, declared once. The alias is what appears in OpenAPI.
LeaseHeader = Annotated[Optional[str], Header(alias="X-Lease-Id")]
AdminTokenHeader = Annotated[Optional[str], Header(alias="X-Admin-Token")]

SessionManagerDep = Annotated[SessionManager, Depends(get_session_manager)]
HeapPoolDep = Annotated[HeapPool, Depends(get_heap_pool)]


def require_lease_id(x_lease_id: Optional[str]) -> str:
    if not x_lease_id:
        raise SessionLeaseError("Missing X-Lease-Id header")
    return x_lease_id


def require_admin_token(x_admin_token: Optional[str]) -> None:
    """403 unless ISABELLE_ADMIN_TOKEN is configured AND matches the header.
    Mismatches are logged (brute-force visibility)."""
    if not Server.ADMIN_TOKEN or x_admin_token != Server.ADMIN_TOKEN:
        logger.warning(
            "admin token %s", "not configured" if not Server.ADMIN_TOKEN else "mismatch"
        )
        raise HTTPException(status_code=403, detail="admin token required")


def safe_segment(value: str, what: str) -> str:
    """Path-parameter form of input_guards.validate_safe_name → 422."""
    try:
        return validate_safe_name(value, what)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))


def leased_session(
    session_id: str,
    x_lease_id: Optional[str] = Header(None, alias="X-Lease-Id"),
    session_manager: SessionManager = Depends(get_session_manager),
) -> _Isabelle_Session:
    """Resolve ``{session_id}`` under its lease and tag the log context.

    The context is set (not reset) on purpose: the request-logging middleware
    runs the handler in its own task, so the tag dies with the request. The
    lease check runs after tagging so a lease error is logged with the id.
    """
    set_logging_context(session_id=session_id)
    lease_id = require_lease_id(x_lease_id)
    return session_manager.get_session(session_id, lease_id=lease_id, require_lease=True)


LeasedSession = Annotated[_Isabelle_Session, Depends(leased_session)]


@asynccontextmanager
async def sledgehammer_slot(session_manager: SessionManager) -> AsyncIterator[None]:
    """Bound concurrent sledgehammers so a burst cannot OOM-kill the gateway
    (ISSUES Bug 6). Extra requests queue here (backpressure) rather than
    oversubscribing; the in-flight gauge covers queue time as well."""
    sem = getattr(session_manager, "sledgehammer_sem", None)
    metrics.sledgehammer_inflight.inc()
    try:
        if sem is not None:
            async with sem:
                yield
        else:
            yield
    finally:
        metrics.sledgehammer_inflight.dec()
