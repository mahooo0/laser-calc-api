"""Flask application for the DXF laser calculator."""

from __future__ import annotations

import logging
import os
import re
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS
from werkzeug.datastructures import FileStorage
from werkzeug.exceptions import RequestEntityTooLarge

from laser_calc import calculate, normalize_layers

from .config import Settings
from .logging_config import configure_logging
from .notifications import TelegramNotifier, order_notification_from_payload
from .services import (
    DEFAULT_TOL_MM,
    append_order_csv,
    catalog_payload,
    collect_preview_shapes,
    load_price_catalog,
    price_coefficients,
    resolve_tariff,
    safe_float,
    safe_int,
)

ALLOWED_FILE_EXTENSIONS = (".dxf",)
MAX_QUANTITY = 100_000
MAX_PHONE_LENGTH = 32
MAX_NAME_LENGTH = 200
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_NON_DIGITS_RE = re.compile(r"\D+")

logger = logging.getLogger(__name__)


def normalize_ua_phone(raw: str) -> str | None:
    """Validate a Ukrainian phone and return it canonicalised as +380XXXXXXXXX.

    Accepts the two real-world input shapes — local ``0XXXXXXXXX`` (10 digits)
    and international ``+380XXXXXXXXX`` / ``380XXXXXXXXX`` (12 digits) — ignoring
    any spaces, dashes or parentheses the user typed. Returns ``None`` for
    anything else so the caller can reject it. National significant number is
    always 9 digits after the operator code, hence the 10/12 length checks.
    """
    digits = _NON_DIGITS_RE.sub("", raw)
    if len(digits) == 10 and digits.startswith("0"):
        return "+380" + digits[1:]
    if len(digits) == 12 and digits.startswith("380"):
        return "+" + digits
    return None


def create_app(
    settings: Settings | None = None,
    *,
    prices_path: Path | None = None,
    orders_path: Path | None = None,
    web_dir: Path | None = None,
    configure_logging_on_init: bool = True,
) -> Flask:
    """Build a Flask application instance.

    Settings come from env by default; tests can either pass an explicit
    Settings object or override individual paths via keyword arguments.
    """
    base = settings or Settings.from_env()
    if configure_logging_on_init:
        configure_logging(base.log_level)
    prices_path = Path(prices_path) if prices_path else base.prices_path
    orders_path = Path(orders_path) if orders_path else base.orders_path
    web_dir = Path(web_dir) if web_dir else base.web_dir

    assets_dir = web_dir / "assets"
    app = Flask(
        __name__,
        static_folder=str(assets_dir) if assets_dir.exists() else None,
        static_url_path="/assets",
    )
    notifier = TelegramNotifier(
        bot_token=base.telegram_bot_token,
        chat_ids=base.telegram_chat_ids,
        enabled=base.telegram_enabled,
        timeout_seconds=base.telegram_timeout_seconds,
    )

    app.config["SETTINGS"] = base
    app.config["PRICES_PATH"] = prices_path
    app.config["ORDERS_PATH"] = orders_path
    app.config["WEB_DIR"] = web_dir
    app.config["MAX_CONTENT_LENGTH"] = base.max_upload_bytes
    app.config["NOTIFIER"] = notifier

    CORS(
        app,
        resources={r"/api/*": {"origins": base.allowed_origins}},
        supports_credentials=False,
    )

    @app.get("/")
    @app.get("/index.html")
    def index() -> Any:
        return send_from_directory(str(web_dir), "index.html")

    @app.get("/api/health")
    def health() -> Any:
        return jsonify({"ok": True})

    @app.get("/api/catalog")
    def catalog() -> Any:
        try:
            cat = load_price_catalog(prices_path)
            return jsonify(catalog_payload(cat))
        except ValueError as ex:
            return jsonify({"ok": False, "error": str(ex)}), 400

    @app.post("/api/calculate")
    def api_calculate() -> Any:
        return _handle_calculate(prices_path, orders_path, notifier)

    @app.errorhandler(RequestEntityTooLarge)
    def _on_too_large(_: RequestEntityTooLarge) -> Any:
        logger.warning(
            "upload_too_large",
            extra={"limit_bytes": app.config["MAX_CONTENT_LENGTH"]},
        )
        return (
            jsonify(
                {
                    "ok": False,
                    "error": (
                        f"Uploaded file exceeds the limit of "
                        f"{app.config['MAX_CONTENT_LENGTH']} bytes"
                    ),
                }
            ),
            413,
        )

    return app


def _bad_request(message: str) -> tuple[Any, int]:
    logger.warning("validation_failed", extra={"reason": message, "path": request.path})
    return jsonify({"ok": False, "error": message}), 400


def _handle_calculate(prices_path: Path, orders_path: Path, notifier: TelegramNotifier) -> Any:
    file: FileStorage | None = request.files.get("file")
    if file is None or not file.filename:
        return _bad_request("DXF file is required")

    file_name = file.filename or "input.dxf"
    if not file_name.lower().endswith(ALLOWED_FILE_EXTENSIONS):
        return _bad_request("Only .dxf files are accepted")

    data = file.stream.read()
    if not data:
        return _bad_request("Uploaded file is empty")

    tol = max(safe_float(request.form.get("tol", str(DEFAULT_TOL_MM)), DEFAULT_TOL_MM), 0.001)
    quantity = max(1, safe_int(request.form.get("quantity", "1"), 1))
    if quantity > MAX_QUANTITY:
        return _bad_request(f"Quantity must be {MAX_QUANTITY} or less")
    metal_grade = (request.form.get("metal_grade") or "").strip()
    metal_thickness = (request.form.get("metal_thickness") or "").strip().replace(",", ".")
    client_name = (request.form.get("client_name") or "").strip()
    company_name = (request.form.get("company_name") or "").strip()
    phone = (request.form.get("phone") or "").strip()
    email = (request.form.get("email") or "").strip()

    if not metal_grade or not metal_thickness:
        return _bad_request("Metal grade and thickness are required")
    if len(client_name) > MAX_NAME_LENGTH or len(company_name) > MAX_NAME_LENGTH:
        return _bad_request(f"Name must be {MAX_NAME_LENGTH} characters or less")
    if not client_name and not company_name:
        return _bad_request("Client name or company name is required")
    if not phone:
        return _bad_request("Phone is required")
    if len(phone) > MAX_PHONE_LENGTH:
        return _bad_request(f"Phone must be {MAX_PHONE_LENGTH} characters or less")
    normalized_phone = normalize_ua_phone(phone)
    if normalized_phone is None:
        return _bad_request("Phone must be a Ukrainian number, e.g. +380XXXXXXXXX")
    phone = normalized_phone
    if not email:
        return _bad_request("Email is required")
    if not EMAIL_RE.match(email):
        return _bad_request("Email format is invalid")

    layers_text = (request.form.get("layers") or "").strip()
    cut_layers = normalize_layers([layers_text]) if layers_text else None

    temp_path: str | None = None
    try:
        price_catalog = load_price_catalog(prices_path)
        # Validate grade/thickness up front; the volume-tier rate is resolved
        # below once we know the batch's total cut length.
        tariff = resolve_tariff(price_catalog, metal_grade, metal_thickness)

        with tempfile.NamedTemporaryFile(delete=False, suffix=".dxf") as tmp:
            temp_path = tmp.name
            tmp.write(data)

        stats, factor = calculate(temp_path, tol=tol, cut_layers=cut_layers)
        # Reject geometry-less uploads (empty drawing, or a cut-layer filter that
        # matched nothing) before booking a 0-грн phantom order to CSV/Telegram.
        if stats.cut_length_mm <= 0 and stats.pierces <= 0:
            return _bad_request(
                "No cuttable geometry found in the DXF (check the cut layer and units)"
            )

        shapes = collect_preview_shapes(temp_path, tol=tol, cut_layers=cut_layers)

        cut_m = stats.cut_length_mm / 1000.0
        area_m2 = stats.area_mm2 / 1_000_000.0
        coeffs = price_coefficients(tariff, cut_m * quantity)
        price_meter = coeffs["price_meter"]
        price_pierce = coeffs["price_pierce"]
        material_m2 = coeffs["material_m2"]
        total_per_part = cut_m * price_meter + stats.pierces * price_pierce + area_m2 * material_m2
        total_batch = total_per_part * quantity
        created_at = datetime.now(UTC).replace(microsecond=0, tzinfo=None).isoformat() + "Z"

        append_order_csv(
            orders_path,
            {
                "created_at_utc": created_at,
                "file_name": file_name,
                "client_name": client_name,
                "company_name": company_name,
                "phone": phone,
                "email": email,
                "metal_grade": metal_grade,
                "metal_thickness_mm": metal_thickness,
                "quantity": quantity,
                "price_meter": price_meter,
                "price_pierce": price_pierce,
                "material_m2": material_m2,
                "cut_length_m_per_part": round(cut_m, 6),
                "pierces_per_part": stats.pierces,
                "area_m2_per_part": round(area_m2, 6),
                "price_total_per_part": round(total_per_part, 2),
                "price_total_batch": round(total_batch, 2),
                "status": "new",
            },
        )

        payload = {
            "ok": True,
            "file_name": file_name,
            "order_saved": True,
            "units_to_mm_factor": factor,
            "client": {
                "client_name": client_name,
                "company_name": company_name,
                "phone": phone,
                "email": email,
            },
            "material": {
                "metal_grade": metal_grade,
                "metal_thickness_mm": metal_thickness,
                "quantity": quantity,
                "tariff": {
                    "price_meter": price_meter,
                    "price_pierce": price_pierce,
                    "material_m2": material_m2,
                    "volume_tier": int(coeffs["tier_index"]),
                    "pierce_equiv_m": round(coeffs["pierce_equiv_m"], 6),
                },
            },
            "metrics": {
                "cut_length_mm": stats.cut_length_mm,
                "cut_length_m": cut_m,
                "pierces": stats.pierces,
                "area_mm2": stats.area_mm2,
                "area_m2": area_m2,
                "unsupported": stats.unsupported,
                "warnings": stats.warnings,
                "price_total_per_part": total_per_part,
                "price_total_batch": total_batch,
            },
            "batch_metrics": {
                "cut_length_mm": stats.cut_length_mm * quantity,
                "cut_length_m": cut_m * quantity,
                "pierces": stats.pierces * quantity,
                "area_mm2": stats.area_mm2 * quantity,
                "area_m2": area_m2 * quantity,
            },
            "preview": {
                "shapes": shapes,
                "shape_count": len(shapes),
            },
        }
        logger.info(
            "calculation_completed",
            extra={
                "file_name": file_name,
                "metal_grade": metal_grade,
                "metal_thickness_mm": metal_thickness,
                "quantity": quantity,
                "cut_length_m": round(cut_m, 6),
                "pierces": stats.pierces,
                "area_m2": round(area_m2, 6),
                "price_total_per_part": round(total_per_part, 2),
                "price_total_batch": round(total_batch, 2),
            },
        )

        try:
            # temp_path still exists here — the finally block removes it only
            # after we return, so the DXF can ride along to Telegram as a
            # document. Nothing is kept on disk: the chat is the archive.
            notifier.notify(
                order_notification_from_payload(payload),
                document_path=temp_path,
                document_name=file_name,
            )
        except Exception:
            # TelegramNotifier swallows its own errors; this catches anything
            # that slips past (programming bugs, malformed payload).
            logger.exception("notifier_unexpected_error", extra={"file_name": file_name})

        return jsonify(payload)
    except ValueError as ex:
        return _bad_request(str(ex))
    except Exception as ex:
        logger.exception("calculation_failed", extra={"file_name": file_name})
        return _bad_request(str(ex))
    finally:
        if temp_path and os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except OSError:
                pass
