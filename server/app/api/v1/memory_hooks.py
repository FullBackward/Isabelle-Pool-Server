"""Compatibility re-export: the hooks live in ``server.app.services.memory_hooks``
(moved so the fill job can use them without importing the API layer)."""

from server.app.services.memory_hooks import (  # noqa: F401
    PROVER_DIED_TEXT,
    first_error_text,
    kind_for,
    run_with_memory_hooks,
    track_atps,
)

__all__ = [
    "PROVER_DIED_TEXT",
    "first_error_text",
    "kind_for",
    "run_with_memory_hooks",
    "track_atps",
]
