"""Pure response-shaping helpers shared by the v1 route modules (no I/O)."""
from typing import Any, Optional

from server.app.core.config import Server
from server.app.core.logging import get_logger
from server.app.services.unicode_normaliser import normalise_for_isabelle

from .schemas.API_models import (
    CommandRange,
    HeapEntryResponse,
    LocatedCommand,
    Position,
)

logger = get_logger(__name__)


def preview(text: Optional[str], limit: int) -> str:
    if not text:
        return ""
    compact = " ".join(str(text).split())
    if len(compact) <= limit:
        return compact
    return compact[: max(0, limit - 3)] + "..."


def to_ascii(text):
    r"""Normalise RENDERED output (goals, state, query/sledgehammer results, hover
    contents) back to Isabelle's \<name> ASCII notation. The PIDE layer decodes
    escapes to Unicode for display; agents are told to write \<...> — so read
    output should speak the same notation. Uses Isabelle's own symbol table via
    normalise_for_isabelle; gated by Server.ASCII_OUTPUT. Raw document source
    (command_at_line.source, GET .../source) must NOT pass through here."""
    if text is None or not Server.ASCII_OUTPUT:
        return text
    try:
        return normalise_for_isabelle(str(text))
    except FileNotFoundError:
        # Host-side dev without an Isabelle install: degrade to raw output.
        logger.warning("Isabelle symbol table not found — returning raw (Unicode) output")
        return str(text)


def parse_command_range(raw) -> Optional[CommandRange]:
    """Parse the optional per-command `range` from a backend chunk report.

    Defensive: any malformed shape (missing start/end, non-int line/col) yields
    None so one bad entry never fails the whole report.
    """
    try:
        if not isinstance(raw, dict):
            return None
        start, end = raw["start"], raw["end"]
        return CommandRange(
            start=Position(line=int(start["line"]), col=int(start["col"])),
            end=Position(line=int(end["line"]), col=int(end["col"])),
        )
    except (KeyError, TypeError, ValueError, AttributeError):
        return None


def parse_located_command(raw) -> Optional[LocatedCommand]:
    """Parse the {kind, source, range} command object of a backend line-query
    reply; None on any malformed shape."""
    try:
        if not isinstance(raw, dict):
            return None
        return LocatedCommand(
            kind=str(raw.get("kind", "")),
            source=str(raw.get("source", "")),
            range=parse_command_range(raw.get("range")),
        )
    except (TypeError, ValueError, AttributeError):
        return None


def session_status(session: Any) -> str:
    status = session.status
    return status.value if hasattr(status, "value") else str(status)


def heap_entry_response(entry) -> HeapEntryResponse:
    return HeapEntryResponse(
        task_group=entry["task_group"],
        project=entry["project"],
        session_name=entry["session_name"],
        root_dir=entry["root_dir"],
        fingerprint=entry["fingerprint"],
        status=entry["status"],
        built_at=entry.get("built_at"),
        built_by=entry.get("built_by"),
        build_log_tail=entry.get("build_log_tail", ""),
    )
