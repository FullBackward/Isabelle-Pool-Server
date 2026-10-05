import contextvars
import logging
import os
from contextlib import contextmanager
from logging.handlers import RotatingFileHandler
from typing import Any, Iterator

from server.app.core.config import Logging

BASE_LOGGER_NAME = "isabelle_pool_server"
_CONTEXT: contextvars.ContextVar[dict[str, str]] = contextvars.ContextVar("log_context", default={})


class _ContextFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        context = _CONTEXT.get()
        record.request_id = context.get("request_id", "-")
        record.session_id = context.get("session_id", "-")
        record.field = context.get("field", "-")
        return True


class _LevelRangeFilter(logging.Filter):
    def __init__(self, min_level: int = logging.NOTSET, max_level: int = logging.CRITICAL):
        super().__init__()
        self.min_level = min_level
        self.max_level = max_level

    def filter(self, record: logging.LogRecord) -> bool:
        return self.min_level <= record.levelno <= self.max_level


class _ExcludeBaseLoggerFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return not record.name.startswith(BASE_LOGGER_NAME)



def _parse_level(level_name: str) -> int:
    return getattr(logging, str(level_name).upper(), logging.INFO)



def set_logging_context(**values: Any):
    current = dict(_CONTEXT.get())
    for key, value in values.items():
        if value is None:
            continue
        current[key] = str(value)
    return _CONTEXT.set(current)



def reset_logging_context(token) -> None:
    _CONTEXT.reset(token)


@contextmanager
def logging_context(**values: Any) -> Iterator[None]:
    token = set_logging_context(**values)
    try:
        yield
    finally:
        reset_logging_context(token)



def _build_formatter() -> logging.Formatter:
    return logging.Formatter(
        fmt=(
            "%(asctime)s %(levelname)s %(name)s "
            "[request_id=%(request_id)s session_id=%(session_id)s field=%(field)s] "
            "%(message)s"
        ),
        datefmt="%Y-%m-%d %H:%M:%S",
    )



def _configure_base_logger() -> logging.Logger:
    level = _parse_level(Logging.LOG_LEVEL)
    base = logging.getLogger(BASE_LOGGER_NAME)
    base.setLevel(level)
    base.propagate = False

    if getattr(base, "_configured", False):
        return base

    formatter = _build_formatter()
    context_filter = _ContextFilter()

    console = logging.StreamHandler()
    console.setLevel(level)
    console.setFormatter(formatter)
    console.addFilter(context_filter)
    base.addHandler(console)

    if Logging.ENABLE_FILE_LOGGING:
        os.makedirs(Logging.LOG_DIR, exist_ok=True)
        file_handler = RotatingFileHandler(
            os.path.join(Logging.LOG_DIR, Logging.LOG_FILE),
            maxBytes=Logging.MAX_LOG_SIZE_BYTES,
            backupCount=Logging.BACKUP_COUNT,
            encoding="utf-8",
        )
        file_handler.setLevel(level)
        file_handler.setFormatter(formatter)
        file_handler.addFilter(context_filter)
        base.addHandler(file_handler)

    base._configured = True
    return base



def _configure_root_fallback(base: logging.Logger) -> None:
    root = logging.getLogger()
    root.setLevel(max(logging.WARNING, base.level))

    if getattr(root, "_configured_by_isabelle", False):
        return

    for handler in base.handlers:
        root.addHandler(handler)
    root.addFilter(_ContextFilter())
    root._configured_by_isabelle = True



def _configure_external_loggers(base: logging.Logger) -> None:
    context_filter = _ContextFilter()
    for logger_name in ("uvicorn", "uvicorn.error", "uvicorn.access", "fastapi"):
        external = logging.getLogger(logger_name)
        external.handlers.clear()
        external.setLevel(base.level)
        external.propagate = True
        external.filters.clear()
        external.addFilter(context_filter)

    # Avoid duplicate output from libraries that also log to the root logger.
    py4j_logger = logging.getLogger("py4j")
    py4j_logger.setLevel(max(base.level, logging.WARNING))
    py4j_logger.propagate = True



def setup_logging() -> None:
    base = _configure_base_logger()
    _configure_root_fallback(base)
    _configure_external_loggers(base)
    logging.captureWarnings(True)



def get_logger(module_name: str) -> logging.Logger:
    setup_logging()
    suffix = module_name
    if suffix.startswith("server.app."):
        suffix = suffix[len("server.app.") :]
    elif suffix.startswith("server."):
        suffix = suffix[len("server.") :]
    logger = logging.getLogger(f"{BASE_LOGGER_NAME}.{suffix}")
    logger.setLevel(logging.NOTSET)
    logger.propagate = True
    return logger
