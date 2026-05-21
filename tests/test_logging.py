"""Unit tests for the JSON log formatter."""

from __future__ import annotations

import json
import logging

from laser_calc_api.logging_config import JsonFormatter


def _make_record(message: str, **extra: object) -> logging.LogRecord:
    record = logging.LogRecord(
        name="laser_calc_api.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=10,
        msg=message,
        args=(),
        exc_info=None,
    )
    for key, value in extra.items():
        setattr(record, key, value)
    return record


def test_formatter_emits_required_top_level_fields() -> None:
    formatter = JsonFormatter()
    record = _make_record("hello")

    line = formatter.format(record)
    payload = json.loads(line)
    assert payload["message"] == "hello"
    assert payload["level"] == "INFO"
    assert payload["logger"] == "laser_calc_api.test"
    assert "timestamp" in payload


def test_formatter_promotes_extras_to_top_level() -> None:
    formatter = JsonFormatter()
    record = _make_record("calc", file_name="x.dxf", pierces=3, price_total_batch=22.92)

    payload = json.loads(formatter.format(record))
    assert payload["file_name"] == "x.dxf"
    assert payload["pierces"] == 3
    assert payload["price_total_batch"] == 22.92


def test_formatter_includes_exception_info() -> None:
    formatter = JsonFormatter()
    try:
        raise ValueError("boom")
    except ValueError:
        import sys

        record = logging.LogRecord(
            name="laser_calc_api.test",
            level=logging.ERROR,
            pathname=__file__,
            lineno=10,
            msg="failed",
            args=(),
            exc_info=sys.exc_info(),
        )

    payload = json.loads(formatter.format(record))
    assert payload["level"] == "ERROR"
    assert "boom" in payload["exc_info"]
    assert "ValueError" in payload["exc_info"]
