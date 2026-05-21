"""Unit tests for the Telegram notifier."""

from __future__ import annotations

import json
import logging
import urllib.error
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from laser_calc_api.notifications import (
    OrderNotification,
    TelegramNotifier,
    order_notification_from_payload,
    render_telegram_message,
)


@pytest.fixture
def order() -> OrderNotification:
    return OrderNotification(
        file_name="part.dxf",
        client_name="Олександр",
        company_name="ООО Ромашка",
        phone="+380501234567",
        email="alex@example.com",
        metal_grade="Ст3",
        metal_thickness_mm="1.0",
        quantity=2,
        cut_length_m_per_part=0.815,
        pierces_per_part=4,
        price_total_per_part=22.92,
        price_total_batch=45.84,
    )


# ---------- render ----------


def test_render_includes_all_core_fields(order: OrderNotification) -> None:
    msg = render_telegram_message(order)
    assert "Олександр" in msg
    assert "ООО Ромашка" in msg
    assert "+380501234567" in msg
    assert "alex@example.com" in msg
    assert "part.dxf" in msg
    assert "Ст3" in msg
    assert "1.0 мм" in msg
    assert "× 2 шт." in msg
    assert "0.815 м" in msg
    assert "4 пробивок" in msg
    assert "22.92 грн" in msg
    assert "45.84 грн" in msg


def test_render_html_escapes_user_input() -> None:
    o = OrderNotification(
        file_name="<script>alert(1)</script>.dxf",
        client_name="Bobby <Drop> & Tables",
        company_name="",
        phone="+380",
        email="x@y.z",
        metal_grade="A&B",
        metal_thickness_mm="1.0",
        quantity=1,
        cut_length_m_per_part=0.0,
        pierces_per_part=0,
        price_total_per_part=0.0,
        price_total_batch=0.0,
    )
    msg = render_telegram_message(o)
    assert "<script>" not in msg
    assert "&lt;script&gt;" in msg
    assert "&amp; Tables" in msg
    assert "A&amp;B" in msg
    # Our own <b> tag must remain intact
    assert "<b>Новая заявка</b>" in msg


def test_render_handles_missing_company(order: OrderNotification) -> None:
    o = OrderNotification(**{**order.__dict__, "company_name": ""})
    msg = render_telegram_message(o)
    assert "Олександр" in msg
    assert "(" not in msg.split("\n")[2]  # the "👤 ..." line has no parens


def test_render_handles_company_only(order: OrderNotification) -> None:
    o = OrderNotification(**{**order.__dict__, "client_name": "", "company_name": "ACME"})
    msg = render_telegram_message(o)
    who_line = next(line for line in msg.split("\n") if line.startswith("👤"))
    assert who_line == "👤 ACME"


def test_render_handles_no_name_at_all(order: OrderNotification) -> None:
    o = OrderNotification(**{**order.__dict__, "client_name": "", "company_name": ""})
    msg = render_telegram_message(o)
    assert "👤 —" in msg


# ---------- is_active ----------


def test_inactive_when_disabled(order: OrderNotification) -> None:
    notifier = TelegramNotifier(bot_token="t", chat_ids=["1"], enabled=False)
    assert notifier.is_active is False


def test_inactive_when_no_token(order: OrderNotification) -> None:
    assert TelegramNotifier(bot_token="", chat_ids=["1"]).is_active is False


def test_inactive_when_no_chat_ids(order: OrderNotification) -> None:
    assert TelegramNotifier(bot_token="t", chat_ids=[]).is_active is False


def test_active_when_all_set() -> None:
    assert TelegramNotifier(bot_token="t", chat_ids=["1"]).is_active is True


# ---------- notify: HTTP behaviour ----------


def _ok_response(status: int = 200, body: bytes = b'{"ok":true}') -> MagicMock:
    resp = MagicMock()
    resp.status = status
    resp.read.return_value = body
    resp.__enter__ = MagicMock(return_value=resp)
    resp.__exit__ = MagicMock(return_value=False)
    return resp


def test_notify_skips_when_inactive(order: OrderNotification) -> None:
    notifier = TelegramNotifier(bot_token="", chat_ids=[])
    with patch("urllib.request.urlopen") as mock_open:
        notifier.notify(order)
    mock_open.assert_not_called()


def test_notify_sends_one_request_per_chat(order: OrderNotification) -> None:
    notifier = TelegramNotifier(bot_token="TOKEN", chat_ids=["123", "-456"])
    with patch("urllib.request.urlopen", return_value=_ok_response()) as mock_open:
        notifier.notify(order)
    assert mock_open.call_count == 2


def test_notify_request_shape(order: OrderNotification) -> None:
    notifier = TelegramNotifier(bot_token="TOKEN", chat_ids=["123"])
    with patch("urllib.request.urlopen", return_value=_ok_response()) as mock_open:
        notifier.notify(order)

    request_arg = mock_open.call_args.args[0]
    assert request_arg.full_url.endswith("/botTOKEN/sendMessage")
    body = json.loads(request_arg.data.decode("utf-8"))
    assert body["chat_id"] == "123"
    assert body["parse_mode"] == "HTML"
    assert body["disable_web_page_preview"] is True
    assert "Олександр" in body["text"]


def test_notify_partial_failure_does_not_block_other_chats(
    order: OrderNotification, caplog: pytest.LogCaptureFixture
) -> None:
    """First chat 500, second chat 200 — second chat must still receive."""
    notifier = TelegramNotifier(bot_token="TOKEN", chat_ids=["bad", "good"])

    def side_effect(req: Any, timeout: float) -> Any:
        body = json.loads(req.data.decode("utf-8"))
        if body["chat_id"] == "bad":
            raise urllib.error.HTTPError(req.full_url, 500, "Internal Server Error", {}, None)
        return _ok_response()

    with caplog.at_level(logging.WARNING, logger="laser_calc_api.notifications"):
        with patch("urllib.request.urlopen", side_effect=side_effect) as mock_open:
            notifier.notify(order)

    assert mock_open.call_count == 2
    failure_records = [r for r in caplog.records if r.message == "telegram_send_failed"]
    assert len(failure_records) == 1
    assert failure_records[0].chat_id == "bad"


def test_notify_swallows_network_error(
    order: OrderNotification, caplog: pytest.LogCaptureFixture
) -> None:
    notifier = TelegramNotifier(bot_token="TOKEN", chat_ids=["123"])
    with caplog.at_level(logging.WARNING, logger="laser_calc_api.notifications"):
        with patch(
            "urllib.request.urlopen",
            side_effect=urllib.error.URLError("connection refused"),
        ):
            notifier.notify(order)  # must not raise

    assert any(r.message == "telegram_unreachable" for r in caplog.records)


def test_notify_swallows_unexpected_exception(
    order: OrderNotification, caplog: pytest.LogCaptureFixture
) -> None:
    notifier = TelegramNotifier(bot_token="TOKEN", chat_ids=["123"])
    with caplog.at_level(logging.ERROR, logger="laser_calc_api.notifications"):
        with patch("urllib.request.urlopen", side_effect=RuntimeError("boom")):
            notifier.notify(order)  # must not raise

    assert any(r.message == "telegram_unexpected_error" for r in caplog.records)


# ---------- payload conversion ----------


def test_order_from_payload_extracts_nested_fields() -> None:
    payload = {
        "file_name": "x.dxf",
        "client": {
            "client_name": "Alice",
            "company_name": "ACME",
            "phone": "+1",
            "email": "a@b.c",
        },
        "material": {
            "metal_grade": "Ст3",
            "metal_thickness_mm": "2.0",
            "quantity": 3,
            "tariff": {},
        },
        "metrics": {
            "cut_length_m": 1.234,
            "pierces": 7,
            "price_total_per_part": 99.5,
            "price_total_batch": 298.5,
        },
    }
    order = order_notification_from_payload(payload)
    assert order.client_name == "Alice"
    assert order.company_name == "ACME"
    assert order.metal_thickness_mm == "2.0"
    assert order.quantity == 3
    assert order.cut_length_m_per_part == 1.234
    assert order.pierces_per_part == 7
    assert order.price_total_per_part == 99.5
    assert order.price_total_batch == 298.5
