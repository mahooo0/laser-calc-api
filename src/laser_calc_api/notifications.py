"""Outbound notifications for new orders.

Right now only Telegram. Designed so additional sinks (Sheets, email, etc.)
can plug in by following the same shape: a class with `notify(order)` that
never raises — failures are logged, the calculation pipeline keeps moving.
"""

from __future__ import annotations

import html
import json
import logging
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

TELEGRAM_API_BASE = "https://api.telegram.org"
DEFAULT_TIMEOUT_SECONDS = 5.0


@dataclass(frozen=True)
class OrderNotification:
    """Flat view of an order, ready to render. Decouples notifier from the
    Flask response payload shape so we can change one without breaking the other."""

    file_name: str
    client_name: str
    company_name: str
    phone: str
    email: str
    metal_grade: str
    metal_thickness_mm: str
    quantity: int
    cut_length_m_per_part: float
    pierces_per_part: int
    price_total_per_part: float
    price_total_batch: float


def render_telegram_message(order: OrderNotification) -> str:
    """Render an order as Telegram-flavoured HTML.

    Uses parse_mode=HTML in the API call: only `<`, `>`, `&` need escaping in
    user-supplied fields, which is much safer than MarkdownV2 (where every
    punctuation char must be escaped).
    """
    e = html.escape

    who_line = e(order.client_name) if order.client_name else ""
    if order.company_name:
        company = e(order.company_name)
        who_line = f"{who_line} ({company})" if who_line else company
    if not who_line:
        who_line = "—"

    lines = [
        "🔥 <b>Новая заявка</b>",
        "",
        f"👤 {who_line}",
        f"📞 {e(order.phone)}",
        f"📧 {e(order.email)}",
        "",
        f"📐 {e(order.file_name)}",
        f"🔩 {e(order.metal_grade)}, {e(order.metal_thickness_mm)} мм × {order.quantity} шт.",
        f"📏 Рез: {order.cut_length_m_per_part:.3f} м · {order.pierces_per_part} пробивок",
        "",
        f"💰 За шт: {order.price_total_per_part:.2f} грн",
        f"💰 Партия: {order.price_total_batch:.2f} грн",
    ]
    return "\n".join(lines)


class TelegramNotifier:
    """Posts each order to one or more Telegram chats via Bot API.

    Failures (network, HTTP 4xx/5xx, missing config) are logged and swallowed.
    The calculation pipeline must never break because Telegram is unhappy.
    """

    def __init__(
        self,
        bot_token: str,
        chat_ids: list[str],
        *,
        enabled: bool = True,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        api_base: str = TELEGRAM_API_BASE,
    ) -> None:
        self._bot_token = bot_token
        self._chat_ids = list(chat_ids)
        self._enabled = enabled
        self._timeout_seconds = timeout_seconds
        self._api_base = api_base.rstrip("/")

    @property
    def is_active(self) -> bool:
        return self._enabled and bool(self._bot_token) and bool(self._chat_ids)

    def notify(self, order: OrderNotification) -> None:
        if not self.is_active:
            logger.debug(
                "telegram_skipped",
                extra={
                    "reason": "inactive",
                    "enabled": self._enabled,
                    "has_token": bool(self._bot_token),
                    "chat_count": len(self._chat_ids),
                },
            )
            return

        message = render_telegram_message(order)
        url = f"{self._api_base}/bot{self._bot_token}/sendMessage"

        for chat_id in self._chat_ids:
            self._send_one(url, chat_id, message)

    def _send_one(self, url: str, chat_id: str, message: str) -> None:
        body = json.dumps(
            {
                "chat_id": chat_id,
                "text": message,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            }
        ).encode("utf-8")

        request = urllib.request.Request(
            url,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        try:
            with urllib.request.urlopen(request, timeout=self._timeout_seconds) as resp:
                status = resp.status
                if 200 <= status < 300:
                    logger.info(
                        "telegram_sent",
                        extra={"chat_id": chat_id, "status": status},
                    )
                    return
                payload = resp.read().decode("utf-8", errors="replace")
                logger.warning(
                    "telegram_send_failed",
                    extra={"chat_id": chat_id, "status": status, "response": payload[:500]},
                )
        except urllib.error.HTTPError as ex:
            payload = _safe_read(ex)
            logger.warning(
                "telegram_send_failed",
                extra={
                    "chat_id": chat_id,
                    "status": ex.code,
                    "response": payload[:500],
                },
            )
        except urllib.error.URLError as ex:
            logger.warning(
                "telegram_unreachable",
                extra={"chat_id": chat_id, "error": str(ex.reason)},
            )
        except Exception as ex:  # noqa: BLE001 — last-resort safety net
            logger.exception(
                "telegram_unexpected_error",
                extra={"chat_id": chat_id, "error_type": type(ex).__name__},
            )


def _safe_read(ex: urllib.error.HTTPError) -> str:
    try:
        raw: bytes | None = ex.read() if hasattr(ex, "read") else None
        return raw.decode("utf-8", errors="replace") if raw else ""
    except Exception:  # noqa: BLE001
        return ""


def order_notification_from_payload(payload: dict[str, Any]) -> OrderNotification:
    """Build OrderNotification from the JSON payload assembled in app.py.

    Keeping this conversion in one place means the caller doesn't reach into
    nested dicts; if the payload shape changes only this function is touched.
    """
    metrics = payload["metrics"]
    material = payload["material"]
    client = payload["client"]
    return OrderNotification(
        file_name=str(payload.get("file_name", "")),
        client_name=str(client.get("client_name", "")),
        company_name=str(client.get("company_name", "")),
        phone=str(client.get("phone", "")),
        email=str(client.get("email", "")),
        metal_grade=str(material.get("metal_grade", "")),
        metal_thickness_mm=str(material.get("metal_thickness_mm", "")),
        quantity=int(material.get("quantity", 0)),
        cut_length_m_per_part=float(metrics.get("cut_length_m", 0.0)),
        pierces_per_part=int(metrics.get("pierces", 0)),
        price_total_per_part=float(metrics.get("price_total_per_part", 0.0)),
        price_total_batch=float(metrics.get("price_total_batch", 0.0)),
    )
