"""Heap pool (Stage 3): verified per-project heaps + task-group tenancy.

Task groups are namespace isolation / accident-proofing, NOT a security
boundary. Hardening (2026-09-22): task_group / session / platform path
segments are validated (422), `project` must live under
ISABELLE_HEAP_ALLOWED_ROOTS (422), and the DESTRUCTIVE endpoints (delete
heap, delete image, delete group) require X-Admin-Token; build stays open.
Project paths contain slashes, so manifest/delete use a `:path` converter:
GET /api/v1/heaps/alpha//tmp/hp1 (note the doubled slash).

Registration order matters: `/heaps/available` and `/heaps/images/{session}`
are declared before the generic `/heaps/{task_group}/{project:path}` routes.
"""
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query

from server.app.core.logging import get_logger, logging_context
from server.app.dependencies import get_heap_pool
from server.app.services.heap_pool import HeapNotFound

from ..deps import HeapPoolDep, require_admin_token, safe_segment
from ..schemas.API_models import (
    AvailableHeapsResponse,
    HeapBuildRequest,
    HeapEntryResponse,
    HeapGroupInfo,
    HeapGroupsResponse,
    HeapListResponse,
    HeapManifestResponse,
    HeapTheoryFile,
)
from ..serializers import heap_entry_response

router = APIRouter(tags=["heaps"])
logger = get_logger(__name__)


@router.post("/api/v1/heaps/build", response_model=HeapEntryResponse)
async def build_heap(request: HeapBuildRequest, heap_pool=Depends(get_heap_pool)):
    """Build or rebuild the heap for (task_group, project). 409 while a build
    for that key is in progress; build passing IS the verification gate."""
    with logging_context():
        logger.info("heap build requested group=%s project=%s", request.task_group, request.project)
        entry = await heap_pool.build(
            request.task_group, request.project, request.session_name,
            built_by=request.task_group,
        )
        return heap_entry_response(entry)


@router.get("/api/v1/heaps", response_model=HeapListResponse)
async def list_heaps(heap_pool: HeapPoolDep, task_group: Optional[str] = Query(None)):
    """Pool listing; omit task_group to list all groups (admin)."""
    with logging_context():
        return HeapListResponse(
            heaps=[heap_entry_response(e) for e in heap_pool.list(task_group)]
        )


@router.get("/api/v1/heaps/available", response_model=AvailableHeapsResponse)
async def list_available_heaps(heap_pool: HeapPoolDep):
    """Admin: every heap image on disk — base session images (user-built, e.g.
    HOL-Analysis, and distribution ones, origin user/distribution) plus
    pool-built images (origin pool). The pool listing above only covers
    pool-built heaps; this is the full "what can sessions start from" view."""
    with logging_context():
        return AvailableHeapsResponse(heaps=heap_pool.list_available_heaps())


@router.get("/api/v1/heaps/{task_group}/{project:path}", response_model=HeapManifestResponse)
async def get_heap_manifest(task_group: str, project: str, heap_pool: HeapPoolDep):
    """Full manifest: theory files with sha256/mtime, ROOT text, fingerprint,
    status, log tail. The project path follows the group segment verbatim
    (`:path` converter) — e.g. /api/v1/heaps/alpha//tmp/hp1."""
    with logging_context():
        safe_segment(task_group, "task_group")
        entry = heap_pool.get(task_group, project)
        if entry is None:
            raise HTTPException(
                status_code=404,
                detail=f"no heap for group {task_group!r} project {project!r}",
            )
        return HeapManifestResponse(
            **heap_entry_response(entry).model_dump(),
            root_text=entry.get("root_text", ""),
            theory_files=[HeapTheoryFile(**f) for f in entry.get("theory_files", [])],
        )


@router.delete("/api/v1/heaps/images/{session}")
async def delete_heap_image(
    session: str,
    platform: Optional[str] = Query(None),
    x_admin_token: Optional[str] = Header(None, alias="X-Admin-Token"),
    heap_pool=Depends(get_heap_pool),
):
    """Admin (X-Admin-Token): delete a base heap image from the USER heaps dir
    (frees disk). Distribution images can never be deleted through this path.
    Declared before the generic {task_group}/{project} DELETE so `images` wins."""
    with logging_context():
        require_admin_token(x_admin_token)
        safe_segment(session, "session")
        if platform is not None:
            safe_segment(platform, "platform")
        try:
            result = heap_pool.delete_heap_image(session, platform)
        except HeapNotFound as e:
            raise HTTPException(status_code=404, detail=str(e))
        logger.warning("heap image deleted via admin token session=%s platform=%s", session, platform)
        return result


@router.delete("/api/v1/heaps/{task_group}/{project:path}")
async def delete_heap(
    task_group: str,
    project: str,
    x_admin_token: Optional[str] = Header(None, alias="X-Admin-Token"),
    heap_pool=Depends(get_heap_pool),
):
    """Admin (X-Admin-Token): remove the heap record (manifest + scratch ROOT).
    The heap image under ~/.isabelle/heaps is GC'd when unreferenced; live
    sessions are unaffected."""
    with logging_context():
        require_admin_token(x_admin_token)
        safe_segment(task_group, "task_group")
        if not heap_pool.delete(task_group, project):
            raise HTTPException(
                status_code=404,
                detail=f"no heap for group {task_group!r} project {project!r}",
            )
        return {"deleted": True, "task_group": task_group, "project": project}


@router.get("/api/v1/heap_groups", response_model=HeapGroupsResponse)
async def list_heap_groups(heap_pool: HeapPoolDep):
    with logging_context():
        return HeapGroupsResponse(
            groups=[HeapGroupInfo(**g) for g in heap_pool.groups()]
        )


@router.delete("/api/v1/heap_groups/{group}")
async def delete_heap_group(
    group: str,
    x_admin_token: Optional[str] = Header(None, alias="X-Admin-Token"),
    heap_pool=Depends(get_heap_pool),
):
    """Admin (X-Admin-Token): delete all of a group's heap records. Does NOT
    kill live sessions (they keep their loaded heaps); blocks new session
    creation against it."""
    with logging_context():
        require_admin_token(x_admin_token)
        safe_segment(group, "task_group")
        removed = heap_pool.delete_group(group)
        logger.warning("heap group deleted via admin token group=%s removed=%s", group, removed)
        return {"deleted": removed, "task_group": group}
