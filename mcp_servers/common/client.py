"""Gym-client plumbing shared by both MCP pools."""
from __future__ import annotations

import asyncio
import json
from typing import Any, Optional

import httpx

from client.async_client import PoolAsyncClient


class GymClientMixin:
    """Lazily creates ONE shared ``PoolAsyncClient`` per pool.

    httpx is safe for concurrent requests on distinct sessions/leases, so one
    client serves every MCP connection. Subclasses call ``_init_client(url,
    timeout)`` from their ``__init__``; tests may inject a fake by assigning
    ``pool._client`` directly (the attribute name is part of the test contract).
    """

    _client: Optional[PoolAsyncClient]
    _client_lock: asyncio.Lock
    _gym_url: str
    _http_timeout: float

    def _init_client(self, gym_url: str, http_timeout: float) -> None:
        self._client = None
        self._client_lock = asyncio.Lock()
        self._gym_url = gym_url
        self._http_timeout = http_timeout

    async def client(self) -> PoolAsyncClient:
        if self._client is None:
            async with self._client_lock:
                if self._client is None:
                    self._client = PoolAsyncClient(self._gym_url, timeout=self._http_timeout)
        return self._client


def is_not_found(exc: BaseException) -> bool:
    """True for an httpx 404 (session evicted / closed server-side)."""
    return (
        isinstance(exc, httpx.HTTPStatusError)
        and exc.response is not None
        and exc.response.status_code == 404
    )


def dump_json(x: Any) -> str:
    """Tool-output JSON (non-serialisable values fall back to str)."""
    return json.dumps(x, default=str)
