"""WSGI entry point for production servers (gunicorn, uwsgi, etc.).

Usage:
    gunicorn laser_calc_api.wsgi:app -b 0.0.0.0:8080 -w 2
"""

from __future__ import annotations

from .app import create_app

app = create_app()
