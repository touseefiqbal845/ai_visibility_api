"""Structured logging with a per-run correlation id.

A pipeline run fans out into a dozen or more LLM and DataForSEO calls. Without a
shared id in every line, reading the logs for one failed run means guessing from
timestamps.
"""

from __future__ import annotations

import json
import logging
import sys
from contextvars import ContextVar

_correlation_id: ContextVar[str | None] = ContextVar("correlation_id", default=None)

_RESERVED = set(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {
    "message",
    "asctime",
    "taskName",
}


def set_correlation_id(value: str | None) -> None:
    _correlation_id.set(value)


def get_correlation_id() -> str | None:
    return _correlation_id.get()


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        correlation_id = get_correlation_id()
        if correlation_id:
            payload["correlation_id"] = correlation_id

        # Anything passed via logger.info(..., extra={...}) rides along.
        for key, value in record.__dict__.items():
            if key not in _RESERVED and not key.startswith("_"):
                payload[key] = value

        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)

        return json.dumps(payload, default=str)


def configure_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())

    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    # Werkzeug's own access log duplicates what we emit and is noisy in JSON form.
    logging.getLogger("werkzeug").setLevel(logging.WARNING)
