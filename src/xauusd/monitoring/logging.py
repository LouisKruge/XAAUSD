"""Structured logging. JSON in production, human-readable in dev.

Every decision cycle carries a correlation id so a single evaluation can be traced
across ingestion, analysis, risk and execution in one grep.
"""

from __future__ import annotations

import logging
import sys
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any

import structlog

_cycle_id: ContextVar[str | None] = ContextVar("cycle_id", default=None)

_SECRET_KEYS = {
    "password",
    "token",
    "api_key",
    "secret",
    "bot_token",
    "smtp_password",
    "fred_api_key",
    "anthropic_api_key",
}


def _redact(_logger: Any, _name: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    """Never let a credential reach a log file."""
    for key in list(event_dict):
        if any(s in key.lower() for s in _SECRET_KEYS):
            event_dict[key] = "***REDACTED***"
    return event_dict


def _add_cycle_id(_logger: Any, _name: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    cid = _cycle_id.get()
    if cid:
        event_dict["cycle_id"] = cid
    return event_dict


def configure_logging(
    level: str = "INFO", json_output: bool = True, log_file: str | None = None
) -> None:
    processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        _add_cycle_id,
        _redact,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]
    processors.append(
        structlog.processors.JSONRenderer()
        if json_output
        else structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty())
    )

    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]
    if log_file:
        from logging.handlers import RotatingFileHandler

        Path(log_file).parent.mkdir(parents=True, exist_ok=True)
        handlers.append(RotatingFileHandler(log_file, maxBytes=50_000_000, backupCount=10))

    logging.basicConfig(
        format="%(message)s",
        level=getattr(logging, level.upper(), logging.INFO),
        handlers=handlers,
        force=True,
    )
    structlog.configure(
        processors=processors,
        wrapper_class=structlog.stdlib.BoundLogger,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)  # type: ignore[no-any-return]


@contextmanager
def cycle_context(cycle_id: str | None = None) -> Iterator[str]:
    """Tag every log line emitted inside this block with one decision-cycle id."""
    cid = cycle_id or uuid.uuid4().hex[:12]
    token = _cycle_id.set(cid)
    try:
        yield cid
    finally:
        _cycle_id.reset(token)


def current_cycle_id() -> str | None:
    return _cycle_id.get()
