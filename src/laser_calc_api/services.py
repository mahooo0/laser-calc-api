"""Pure helpers for the laser_calc HTTP layer.

Houses the helpers that back the Flask routes: CSV layout, catalog validation,
preview shape collection, and the cutting-price math. Pricing follows the
client's "Прайс порізка" sheet — a per-metre cut rate chosen by the batch's
volume tier, with each pierce billed as its equivalent cut length. Material
cost is left out until the client supplies a per-kg/per-m² price (see
``price_coefficients`` and ``DEFAULT_PRICE_CATALOG``).
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

# Catalog shape: grade -> thickness -> tariff, where a tariff is:
#   price_meter: [>=100, 50-100, 10-50, <10] м/п  (грн з ПДВ, cut price by volume tier)
#   feed_mm_min: cutting feed, used to price each pierce as equivalent cut length
#   pierce_time_s: pierce duration
#   material_m2: optional metal cost per m² (0 = not priced; the source sheet has
#     no per-kg metal price, so cutting-only until the client supplies one)
Tariff = dict[str, Any]
Catalog = dict[str, dict[str, Tariff]]

# Volume tier thresholds (running metres of cut in the batch), descending.
# total_cut_m >= 100 -> index 0; >= 50 -> 1; >= 10 -> 2; else -> 3.
TIER_THRESHOLDS_M: tuple[float, ...] = (100.0, 50.0, 10.0)

DEFAULT_PRICE_CATALOG: Catalog = {
    "Ст3": {
        "1.0": {
            "price_meter": [9.62, 11.4, 15.21, 22.82],
            "feed_mm_min": 30000,
            "pierce_time_s": 1.2,
            "material_m2": 0,
        },
        "2.0": {
            "price_meter": [19.22, 22.79, 30.38, 45.57],
            "feed_mm_min": 12000,
            "pierce_time_s": 1.4,
            "material_m2": 0,
        },
    },
    "Нерж AISI 304": {
        "1.0": {
            "price_meter": [25.1, 34.22, 45.64, 68.45],
            "feed_mm_min": 35000,
            "pierce_time_s": 1.1,
            "material_m2": 0,
        },
        "2.0": {
            "price_meter": [50.14, 68.36, 91.15, 136.72],
            "feed_mm_min": 17000,
            "pierce_time_s": 1.3,
            "material_m2": 0,
        },
    },
    "Алюміній": {
        "1.0": {
            "price_meter": [18.36, 27.62, 36.82, 55.23],
            "feed_mm_min": 35000,
            "pierce_time_s": 1.1,
            "material_m2": 0,
        },
        "2.0": {
            "price_meter": [58.35, 87.53, 116.7, 175.05],
            "feed_mm_min": 17000,
            "pierce_time_s": 1.3,
            "material_m2": 0,
        },
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


def _coerce_price_meter(value: Any) -> list[float]:
    """Normalize price_meter into a 4-element tier list [>=100, 50-100, 10-50, <10].

    A bare number is treated as a flat price (all tiers equal) so a future
    fixed-price file from the client drops in without code changes.
    """
    if isinstance(value, bool):
        raise ValueError("price_meter must be a number or list")
    if isinstance(value, (int, float)):
        return [float(value)] * 4
    if isinstance(value, (list, tuple)):
        nums = [float(x) for x in value]
        if not nums:
            raise ValueError("price_meter list is empty")
        while len(nums) < 4:
            nums.append(nums[-1])
        return nums[:4]
    raise ValueError("price_meter must be a number or list")


def validate_price_catalog(raw: dict[str, Any]) -> Catalog:
    if not isinstance(raw, dict):
        raise ValueError("Price catalog must be an object")
    data = raw.get("grades", raw)
    if not isinstance(data, dict):
        raise ValueError("Price catalog must have 'grades' object")
    if not data:
        raise ValueError("Price catalog is empty")

    clean: Catalog = {}
    for grade, thickness_map in data.items():
        grade_name = str(grade).strip()
        if grade_name == "" or grade_name.startswith("_"):
            continue
        if not isinstance(thickness_map, dict):
            raise ValueError(f"Invalid grade section for: {grade_name}")

        clean_thickness: dict[str, Tariff] = {}
        for thickness, tariff in thickness_map.items():
            key = normalize_thickness_key(str(thickness))
            if key == "":
                continue
            if not isinstance(tariff, dict):
                raise ValueError(f"Invalid tariff for {grade_name} / {key}")
            try:
                clean_thickness[key] = {
                    "price_meter": _coerce_price_meter(tariff["price_meter"]),
                    "feed_mm_min": float(tariff.get("feed_mm_min", 0) or 0),
                    "pierce_time_s": float(tariff.get("pierce_time_s", 0) or 0),
                    "material_m2": float(tariff.get("material_m2", 0) or 0),
                }
            except (KeyError, TypeError, ValueError) as ex:
                raise ValueError(f"Invalid numeric tariff for {grade_name} / {key}") from ex

        if clean_thickness:
            clean[grade_name] = clean_thickness

    if not clean:
        raise ValueError("No valid tariffs found in price catalog")
    return clean


def load_price_catalog(path: Path) -> Catalog:
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


def catalog_payload(price_catalog: Catalog) -> dict[str, Any]:
    grades: list[dict[str, Any]] = []
    for grade, thickness_map in price_catalog.items():
        thicknesses = sorted(thickness_map.keys(), key=lambda x: float(x))
        grades.append({"metal_grade": grade, "thicknesses": thicknesses})
    return {"ok": True, "grades": grades}


def resolve_tariff(
    price_catalog: Catalog,
    metal_grade: str,
    metal_thickness: str,
) -> Tariff:
    grade_map = price_catalog.get(metal_grade)
    if not grade_map:
        raise ValueError("Unknown metal grade")
    normalized_key = normalize_thickness_key(metal_thickness)
    tariff = grade_map.get(normalized_key) or grade_map.get(metal_thickness)
    if not tariff:
        raise ValueError("Unknown metal thickness for selected grade")
    return tariff


def select_tier_index(total_cut_m: float) -> int:
    """Pick the volume tier (index into price_meter[]) from total batch metres."""
    for index, threshold in enumerate(TIER_THRESHOLDS_M):
        if total_cut_m >= threshold:
            return index
    return len(TIER_THRESHOLDS_M)


def price_coefficients(tariff: Tariff, total_cut_m: float) -> dict[str, float]:
    """Resolve the per-part pricing coefficients for a given batch volume.

    Returns the same three coefficients the legacy flat formula expects, so the
    cost stays ``cut_m·price_meter + pierces·price_pierce + area_m2·material_m2``:

    * ``price_meter`` — the cut rate for the volume tier the batch falls into.
    * ``price_pierce`` — each pierce billed as its equivalent cut length
      (``pierce_time_s × feed_mm_min``) at that tier's rate. Zero when the source
      sheet has no feed/pierce data for the material.
    * ``material_m2`` — metal cost per m² (0 until the client supplies one).
    """
    prices = _coerce_price_meter(tariff["price_meter"])
    tier = select_tier_index(total_cut_m)
    price_meter = prices[tier]
    feed = float(tariff.get("feed_mm_min", 0.0) or 0.0)
    pierce_time = float(tariff.get("pierce_time_s", 0.0) or 0.0)
    pierce_equiv_m = pierce_time * feed / 60.0 / 1000.0
    return {
        "price_meter": price_meter,
        "price_pierce": pierce_equiv_m * price_meter,
        "material_m2": float(tariff.get("material_m2", 0.0) or 0.0),
        "tier_index": float(tier),
        "pierce_equiv_m": pierce_equiv_m,
    }


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
