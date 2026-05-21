"""JSON-line logging for stdout. Docker- and grep-friendly."""

from __future__ import annotations

import json
import logging
import logging.config
from datetime import UTC, datetime
from typing import Any

_RESERVED = {
    "name",
    "msg",
    "args",
    "levelname",
    "levelno",
    "pathname",
    "filename",
    "module",
    "exc_info",
    "exc_text",
    "stack_info",
    "lineno",
    "funcName",
    "created",
    "msecs",
    "relativeCreated",
    "thread",
    "threadName",
    "processName",
    "process",
    "taskName",
    "message",
    "asctime",
}


class JsonFormatter(logging.Formatter):
    """Render log records as a single JSON line.

    Extra fields passed via ``logger.info("msg", extra={"key": "value"})``
    appear at the top level alongside the standard timestamp/level/logger.
    """

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key in _RESERVED or key.startswith("_"):
                continue
            payload[key] = value
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


def configure_logging(level: str = "INFO") -> None:
    """Configure root, werkzeug, and application loggers to emit JSON lines."""
    logging.config.dictConfig(
        {
            "version": 1,
            "disable_existing_loggers": False,
            "formatters": {
                "json": {
                    "()": "laser_calc_api.logging_config.JsonFormatter",
                }
            },
            "handlers": {
                "stdout": {
                    "class": "logging.StreamHandler",
                    "formatter": "json",
                    "stream": "ext://sys.stdout",
                }
            },
            "root": {
                "level": level.upper(),
                "handlers": ["stdout"],
            },
            "loggers": {
                "werkzeug": {
                    "level": level.upper(),
                    "handlers": ["stdout"],
                    "propagate": False,
                },
                "laser_calc_api": {
                    "level": level.upper(),
                    "handlers": ["stdout"],
                    "propagate": False,
                },
            },
        }
    )
