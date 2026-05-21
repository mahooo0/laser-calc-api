"""Pure helpers for the laser_calc HTTP layer.

This module is the migration target for the helpers that previously lived
inside web_app.py. Behavior is preserved verbatim — pricing math, CSV layout,
catalog validation, and preview shape collection match the legacy implementation.
"""

from __future__ import annotations

import csv
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from laser_calc import (
    CalcStats,
    entity_is_closed,
    flatten_entity_points,
    is_supported,
    iter_flat_entities,
    load_doc,
)

Point2D = tuple[float, float]

MAX_SHAPES = 2000
MAX_POINTS_TOTAL = 100_000
MAX_POINTS_PER_SHAPE = 1200
DEFAULT_TOL_MM = 0.05

ORDERS_CSV_HEADERS: tuple[str, ...] = (
    "created_at_utc",
    "file_name",
    "client_name",
    "company_name",
    "phone",
    "email",
    "metal_grade",
    "metal_thickness_mm",
    "quantity",
    "price_meter",
    "price_pierce",
    "material_m2",
    "cut_length_m_per_part",
    "pierces_per_part",
    "area_m2_per_part",
    "price_total_per_part",
    "price_total_batch",
    "status",
)

DEFAULT_PRICE_CATALOG: dict[str, dict[str, dict[str, float]]] = {
    "Ст3": {
        "1.0": {"price_meter": 20.0, "price_pierce": 1.8, "material_m2": 740.0},
        "2.0": {"price_meter": 24.0, "price_pierce": 2.2, "material_m2": 920.0},
        "3.0": {"price_meter": 28.0, "price_pierce": 2.7, "material_m2": 1150.0},
        "4.0": {"price_meter": 34.0, "price_pierce": 3.2, "material_m2": 1420.0},
        "6.0": {"price_meter": 44.0, "price_pierce": 4.2, "material_m2": 2060.0},
    },
    "Нерж AISI 304": {
        "1.0": {"price_meter": 34.0, "price_pierce": 2.7, "material_m2": 1780.0},
        "2.0": {"price_meter": 42.0, "price_pierce": 3.6, "material_m2": 2460.0},
        "3.0": {"price_meter": 55.0, "price_pierce": 4.8, "material_m2": 3380.0},
        "4.0": {"price_meter": 72.0, "price_pierce": 6.2, "material_m2": 4310.0},
    },
    "Алюминий AMg": {
        "2.0": {"price_meter": 38.0, "price_pierce": 3.1, "material_m2": 1670.0},
        "3.0": {"price_meter": 46.0, "price_pierce": 3.9, "material_m2": 2140.0},
        "4.0": {"price_meter": 59.0, "price_pierce": 5.0, "material_m2": 2790.0},
        "6.0": {"price_meter": 79.0, "price_pierce": 6.6, "material_m2": 4020.0},
    },
}


def safe_float(value: str | None, default: float = 0.0) -> float:
    if value is None:
        return default
    text = str(value).strip().replace(",", ".")
    if text == "":
        return default
    try:
        return float(text)
    except ValueError:
        return default


def safe_int(value: str | None, default: int = 1) -> int:
    if value is None:
        return default
    text = str(value).strip()
    if text == "":
        return default
    try:
        return int(float(text))
    except ValueError:
        return default


def normalize_thickness_key(value: str) -> str:
    text = str(value).strip().replace(",", ".")
    if text == "":
        return text
    try:
        number = float(text)
    except ValueError:
        return text
    normalized = f"{number:.4f}".rstrip("0").rstrip(".")
    if "." not in normalized:
        normalized = f"{normalized}.0"
    return normalized


def validate_price_catalog(raw: dict[str, Any]) -> dict[str, dict[str, dict[str, float]]]:
    if not isinstance(raw, dict):
        raise ValueError("Price catalog must be an object")
    data = raw.get("grades", raw)
    if not isinstance(data, dict):
        raise ValueError("Price catalog must have 'grades' object")
    if not data:
        raise ValueError("Price catalog is empty")

    clean: dict[str, dict[str, dict[str, float]]] = {}
    for grade, thickness_map in data.items():
        grade_name = str(grade).strip()
        if grade_name == "":
            continue
        if not isinstance(thickness_map, dict):
            raise ValueError(f"Invalid grade section for: {grade_name}")

        clean_thickness: dict[str, dict[str, float]] = {}
        for thickness, tariff in thickness_map.items():
            key = normalize_thickness_key(str(thickness))
            if key == "":
                continue
            if not isinstance(tariff, dict):
                raise ValueError(f"Invalid tariff for {grade_name} / {key}")
            try:
                clean_thickness[key] = {
                    "price_meter": float(tariff["price_meter"]),
                    "price_pierce": float(tariff["price_pierce"]),
                    "material_m2": float(tariff["material_m2"]),
                }
            except (KeyError, TypeError, ValueError) as ex:
                raise ValueError(f"Invalid numeric tariff for {grade_name} / {key}") from ex

        if clean_thickness:
            clean[grade_name] = clean_thickness

    if not clean:
        raise ValueError("No valid tariffs found in price catalog")
    return clean


def load_price_catalog(path: Path) -> dict[str, dict[str, dict[str, float]]]:
    if path.exists():
        try:
            with path.open("r", encoding="utf-8") as f:
                raw = json.load(f)
            return validate_price_catalog(raw)
        except ValueError:
            raise
        except Exception as ex:
            raise ValueError(f"Invalid prices_catalog.json: {ex}") from ex
    return DEFAULT_PRICE_CATALOG


def catalog_payload(price_catalog: dict[str, dict[str, dict[str, float]]]) -> dict[str, Any]:
    grades: list[dict[str, Any]] = []
    for grade, thickness_map in price_catalog.items():
        thicknesses = sorted(thickness_map.keys(), key=lambda x: float(x))
        grades.append({"metal_grade": grade, "thicknesses": thicknesses})
    return {"ok": True, "grades": grades}


def resolve_tariff(
    price_catalog: dict[str, dict[str, dict[str, float]]],
    metal_grade: str,
    metal_thickness: str,
) -> dict[str, float]:
    grade_map = price_catalog.get(metal_grade)
    if not grade_map:
        raise ValueError("Unknown metal grade")
    normalized_key = normalize_thickness_key(metal_thickness)
    tariff = grade_map.get(normalized_key) or grade_map.get(metal_thickness)
    if not tariff:
        raise ValueError("Unknown metal thickness for selected grade")
    return tariff


def _sanitize_csv_cell(value: object) -> str:
    text = str(value if value is not None else "").strip()
    return " ".join(text.splitlines())


def append_order_csv(path: Path, row: dict[str, object]) -> None:
    file_exists = path.exists()
    with path.open("a", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=ORDERS_CSV_HEADERS)
        if not file_exists or path.stat().st_size == 0:
            writer.writeheader()
        writer.writerow({k: _sanitize_csv_cell(row.get(k, "")) for k in ORDERS_CSV_HEADERS})


def _decimate_points(points: Sequence[Point2D], limit: int) -> list[Point2D]:
    if len(points) <= limit:
        return list(points)
    step = max(1, len(points) // limit)
    sliced = list(points[::step])
    if points[-1] != sliced[-1]:
        sliced.append(points[-1])
    return sliced


def collect_preview_shapes(
    file_path: str,
    tol: float,
    cut_layers: set[str] | None,
) -> list[dict[str, Any]]:
    doc = load_doc(file_path)
    msp = doc.modelspace()
    stats = CalcStats()

    shapes: list[dict[str, Any]] = []
    points_budget = MAX_POINTS_TOTAL
    close_tol = max(tol, 0.05)

    for e in msp:
        for ent in iter_flat_entities(e, stats):
            layer = str(getattr(ent.dxf, "layer", "0")).lower()
            if cut_layers and layer not in cut_layers:
                continue
            if not is_supported(ent):
                continue

            raw_points = flatten_entity_points(ent, tol)
            if len(raw_points) < 2:
                continue

            closed = entity_is_closed(ent, raw_points, close_tol)
            points = _decimate_points(raw_points, MAX_POINTS_PER_SHAPE)

            if points_budget <= 0 or len(shapes) >= MAX_SHAPES:
                return shapes
            if len(points) > points_budget:
                points = points[:points_budget]
            points_budget -= len(points)

            shapes.append(
                {
                    "closed": bool(closed),
                    "points": [[round(x, 4), round(y, 4)] for x, y in points],
                }
            )

    return shapes
