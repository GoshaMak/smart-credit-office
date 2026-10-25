"""Safe JSON logging for backend application processes."""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any


_SAFE_FIELDS = (
    "event",
    "request_id",
    "job_id",
    "application_id",
    "snapshot_id",
    "source",
    "attempt",
    "error_code",
    "method",
    "path",
    "status",
    "operation_id",
    "violation_count",
    "sqlstate",
)


class JsonLogFormatter(logging.Formatter):
    """Serialize only explicit, low-risk fields; omit exception messages and locals."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, timezone.utc)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for name in _SAFE_FIELDS:
            value = getattr(record, name, None)
            if isinstance(value, (str, int)) and not isinstance(value, bool):
                payload[name] = value
        if record.exc_info and record.exc_info[0]:
            payload["exception_type"] = record.exc_info[0].__name__
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def configure_logging() -> None:
    """Configure only the ``credit`` logger namespace with safe JSON output."""
    level_name = os.getenv("LOG_LEVEL", "INFO").upper()
    level = logging.getLevelNamesMapping().get(level_name, logging.INFO)
    logger = logging.getLogger("credit")
    logger.setLevel(level)
    logger.propagate = False

    marked = [
        handler
        for handler in logger.handlers
        if getattr(handler, "_credit_json_handler", False)
    ]
    if marked:
        handler = marked[0]
        for duplicate in marked[1:]:
            logger.removeHandler(duplicate)
    else:
        handler = logging.StreamHandler()
        handler._credit_json_handler = True  # type: ignore[attr-defined]
        logger.addHandler(handler)
    handler.setFormatter(JsonLogFormatter())
