"""Shared pieces of the two MCP servers: env-driven config, the gym-client
mixin, the session registry, memory advice and the per-tool guard."""

from .client import GymClientMixin, dump_json, is_not_found
from .config import env_bool, env_float, env_int, env_str
from .guard import make_guard
from .memory_advice import (
    ToolNames,
    advise,
    others_summary,
    parse_http_error,
    reminder,
    translate_http_error,
    with_reminder,
)
from .registry import (
    KIND_BATCH,
    KIND_BINDING,
    KIND_SCRATCH,
    KIND_THEORY,
    Entry,
    SessionRegistry,
)

__all__ = [
    "GymClientMixin",
    "dump_json",
    "is_not_found",
    "env_bool",
    "env_float",
    "env_int",
    "env_str",
    "make_guard",
    "ToolNames",
    "advise",
    "others_summary",
    "parse_http_error",
    "reminder",
    "translate_http_error",
    "with_reminder",
    "KIND_BATCH",
    "KIND_BINDING",
    "KIND_SCRATCH",
    "KIND_THEORY",
    "Entry",
    "SessionRegistry",
]
