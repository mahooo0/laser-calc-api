"""Gunicorn configuration with JSON logging routed through the app's formatter."""

from __future__ import annotations

import os

bind = f"0.0.0.0:{os.getenv('PORT', '8080')}"
workers = int(os.getenv("GUNICORN_WORKERS", "2"))
worker_class = "sync"
timeout = int(os.getenv("GUNICORN_TIMEOUT", "60"))
graceful_timeout = 30
keepalive = 5

accesslog = "-"
errorlog = "-"

_LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
loglevel = _LOG_LEVEL.lower()

logconfig_dict = {
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
        "level": _LOG_LEVEL,
        "handlers": ["stdout"],
    },
    "loggers": {
        "gunicorn.error": {
            "level": _LOG_LEVEL,
            "handlers": ["stdout"],
            "propagate": False,
        },
        "gunicorn.access": {
            "level": "INFO",
            "handlers": ["stdout"],
            "propagate": False,
        },
        "laser_calc_api": {
            "level": _LOG_LEVEL,
            "handlers": ["stdout"],
            "propagate": False,
        },
    },
}
