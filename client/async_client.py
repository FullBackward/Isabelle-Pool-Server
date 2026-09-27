"""IsabelleGym async HTTP client (httpx only; never imports server code).

``IsabelleGymAsyncClient`` is composed from one mixin per concern so each file
stays small; the public surface and import path are unchanged:

    from client.async_client import IsabelleGymAsyncClient

- ``sessions``   create / acquire / release / close / enter_theory / listings
- ``execution``  execute_command / diagnostic / verify_chunk / load_document /
                 sledgehammer(_at) / bigstep / chunk-report rendering
- ``inspection`` proof state, source, facts, positional queries, checkpoints
- ``heaps``      heap pool + parse_theory_header
"""
from __future__ import annotations

from ._base import BASE_URL, HEAP_GROUPS_URL, HEAPS_URL, THEORY_RE, extract_theory_name
from .execution import ExecutionMixin
from .heaps import HeapsMixin
from .inspection import InspectionMixin
from .sessions import SessionsMixin

__all__ = [
    "IsabelleGymAsyncClient",
    "extract_theory_name",
    "THEORY_RE",
    "BASE_URL",
    "HEAPS_URL",
    "HEAP_GROUPS_URL",
]


class IsabelleGymAsyncClient(SessionsMixin, ExecutionMixin, InspectionMixin, HeapsMixin):
    """Async client for the IsabelleGym server. See the mixin modules for the
    method groups; construct with ``IsabelleGymAsyncClient(base_url, timeout)``."""
