from __future__ import annotations


class SessionError(Exception):

    def __init__(
        self,
        message: str | None = None,
        *,
        error: str | None = None,
        execution_time: float | None = None,
    ) -> None:
        detail = error or message or self.__class__.__name__
        super().__init__(detail)
        self.error = detail
        self.execution_time = execution_time


class SessionNotFound(SessionError):
    pass


class SessionStartError(SessionError):
    pass


class GatewayUnavailable(SessionError):
    pass


class PoolExhausted(SessionError):
    """Raised when the session pool is full and all sessions are actively
    processing requests, so no eviction candidate is available."""
    pass


class MemoryPressure(PoolExhausted):
    """Raised when memory admission refuses new work (session create/acquire,
    big-step or heap build). Carries the structured facts the 503 body shows:
    numbers, state, and the sessions involved — never lease ids. The wording of
    any advice is the client's (MCP's) job (owner decision D4)."""

    def __init__(
        self,
        message: str | None = None,
        *,
        facts: dict[str, object] | None = None,
        retry_after_s: int = 5,
        **kwargs: object,
    ) -> None:
        super().__init__(message, **kwargs)  # type: ignore[arg-type]
        self.facts: dict[str, object] = dict(facts or {})
        self.retry_after_s = retry_after_s


class SessionLeaseError(SessionError):
    """Raised when a caller uses an invalid or missing lease token."""
    pass


class SessionBusyError(SessionError):
    """Raised when a session cannot be released/closed because work is in flight."""
    pass
