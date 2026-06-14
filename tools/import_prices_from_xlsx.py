#!/usr/bin/env python3
"""Build prices_catalog.json from the GT Metal cutting price sheet.

Source: the client's Excel template, sheet "Прайс порізка". Each priced row
carries the cut price for one (material, thickness) in four volume tiers plus
the cutting feed and pierce time. We collapse the client's internal material
designations into calculator grades and emit the catalog the API consumes.

Re-run this whenever the client sends an updated price sheet:

    .venv/bin/python tools/import_prices_from_xlsx.py \
        --xlsx ~/Downloads/Шаблон_прорахунку_лазер_Laser_Cutting.xlsx \
        --out prices_catalog.json

Notes
-----
* Tiers (грн з ПДВ, per metre of cut), in this order: [>=100, 50-100, 10-50, <10] м/п.
* Material cost is NOT in this sheet (грн/кг is a per-order market input), so
  ``material_m2`` is left at 0. When the client supplies a fixed material price
  it drops straight into the JSON — no code change.
* "По запросу" materials (no cut price in the sheet) are skipped on purpose.
"""

from __future__ import annotations

import argparse
import json
import re
from collections import OrderedDict
from pathlib import Path

import openpyxl

SHEET = "Прайс порізка"
FIRST_DATA_ROW = 4
# Column indices (1-based) on the "Прайс порізка" sheet.
COL_NAME = 1  # A: material + internal designation + size
COL_THICKNESS = 2  # B: thickness, mm
COL_TIER_100 = 4  # D: >=100 м/п
COL_TIER_50 = 5  # E: 50-100 м/п
COL_TIER_10 = 6  # F: 10-50 м/п
COL_TIER_0 = 7  # G: <10 м/п
COL_FEED = 9  # I: подача/резка, mm/min
COL_PIERCE = 10  # J: час врізки, s

# Grade display order in the dropdown.
GRADE_ORDER = [
    "Ст3",
    "Оцинкована сталь",
    "09Г2С",
    "Нерж AISI 304",
    "Нерж AISI 430",
    "Алюміній",
    "Дюраль",
    "Латунь/бронза",
    "Мідь",
    "Метал замовника (кисень)",
    "Метал замовника (азот)",
]


def map_grade(prefix: str) -> str | None:
    """Collapse an internal material designation into a calculator grade.

    Returns None for materials we deliberately exclude (priced-on-request).
    Order matters: check the more specific patterns first.
    """
    p = prefix.lower()
    if "заказчика" in p:
        if "кисло" in p:
            return "Метал замовника (кисень)"
        if "азот" in p:
            return "Метал замовника (азот)"
        return None
    if "оц" in p and "08кп" in p:
        return "Оцинкована сталь"
    if "08кп" in p or "3пс" in p:  # х/к (08кп), г/к (3пс), г/к (3пс5)
        return "Ст3"
    if "09г2с" in p:
        return "09Г2С"
    if "304" in p:
        return "Нерж AISI 304"
    if "430" in p:
        return "Нерж AISI 430"
    if "дюраль" in p or "дюралюмин" in p:  # before "алюмин" — substring overlap
        return "Дюраль"
    if "алюмин" in p:
        return "Алюміній"
    if "латунь" in p or "бронза" in p:
        return "Латунь/бронза"
    if "медь" in p:
        return "Мідь"
    return None


def strip_size(name: str) -> str:
    """Drop the trailing "0,50 mm" / "0,5" size from a material cell."""
    s = re.sub(r"[\d.,]+\s*mm\s*$", "", name, flags=re.IGNORECASE)
    s = re.sub(r"[\d.,]+\s*$", "", s)
    return re.sub(r"\s+", " ", s).strip()


def norm_thickness(value: float) -> str:
    normalized = f"{float(value):.4f}".rstrip("0").rstrip(".")
    if "." not in normalized:
        normalized = f"{normalized}.0"
    return normalized


def num(cell) -> float | None:
    if cell is None:
        return None
    try:
        return float(cell)
    except (TypeError, ValueError):
        return None


def build_catalog(xlsx_path: Path) -> dict:
    wb = openpyxl.load_workbook(xlsx_path, data_only=True, read_only=True)
    ws = wb[SHEET]

    grades: OrderedDict[str, dict] = OrderedDict()
    skipped: dict[str, int] = {}
    conflicts: list[str] = []
    zero_pierce: list[str] = []  # priced grades whose pierce ends up free (no feed/pierce in sheet)

    for row in ws.iter_rows(min_row=FIRST_DATA_ROW, values_only=True):
        name = row[COL_NAME - 1]
        if not name:
            continue
        d = num(row[COL_TIER_100 - 1])
        if d is None:  # no cut price -> priced on request, skip
            grp = strip_size(str(name))
            skipped[grp] = skipped.get(grp, 0) + 1
            continue

        grade = map_grade(strip_size(str(name)))
        if grade is None:
            grp = strip_size(str(name))
            skipped[grp] = skipped.get(grp, 0) + 1
            continue

        thickness = num(row[COL_THICKNESS - 1])
        if thickness is None:
            continue
        key = norm_thickness(thickness)

        e = num(row[COL_TIER_50 - 1])
        f = num(row[COL_TIER_10 - 1])
        g = num(row[COL_TIER_0 - 1])
        tiers = [d, e if e is not None else d, f if f is not None else d, g if g is not None else d]
        tariff = {
            "price_meter": [round(x, 2) for x in tiers],
            "feed_mm_min": num(row[COL_FEED - 1]) or 0,
            "pierce_time_s": num(row[COL_PIERCE - 1]) or 0,
            "material_m2": 0,
        }

        if not tariff["feed_mm_min"] or not tariff["pierce_time_s"]:
            zero_pierce.append(f"{grade} {key}")

        bucket = grades.setdefault(grade, {})
        if key in bucket:
            if bucket[key]["price_meter"] != tariff["price_meter"]:
                conflicts.append(
                    f"{grade} {key}: {bucket[key]['price_meter']} vs {tariff['price_meter']}"
                )
            continue  # first-wins on duplicate thickness
        bucket[key] = tariff

    # Order grades and thicknesses deterministically.
    ordered = OrderedDict()
    for grade in GRADE_ORDER:
        if grade in grades:
            tmap = grades[grade]
            ordered[grade] = OrderedDict(sorted(tmap.items(), key=lambda kv: float(kv[0])))
    # Any grade not in GRADE_ORDER (shouldn't happen) goes at the end.
    for grade, tmap in grades.items():
        if grade not in ordered:
            ordered[grade] = OrderedDict(sorted(tmap.items(), key=lambda kv: float(kv[0])))

    return {
        "meta": {
            "currency": "UAH",
            "vat_included": True,
            "source": "GT Metal — sheet 'Прайс порізка'",
            "tiers_m": [100, 50, 10, 0],
            "tiers_note": "price_meter = [>=100, 50-100, 10-50, <10] м/п",
            "material_note": "material_m2=0: sheet prices cutting only; no per-kg metal price in source",
            "pierce_note": (
                "pierce billed as час_врізки×подача at the tier rate; grades with no feed/pierce "
                "in the sheet (e.g. Дюраль) get feed_mm_min=0 -> pierces free until the client confirms"
            ),
        },
        "grades": ordered,
        "_skipped_on_request": dict(sorted(skipped.items())),
        "_conflicts": conflicts,
        "_zero_pierce": sorted(zero_pierce),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--xlsx", required=True, type=Path)
    ap.add_argument("--out", required=True, type=Path)
    args = ap.parse_args()

    catalog = build_catalog(args.xlsx)
    skipped = catalog.pop("_skipped_on_request")
    conflicts = catalog.pop("_conflicts")
    zero_pierce = catalog.pop("_zero_pierce")

    args.out.write_text(json.dumps(catalog, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    grades = catalog["grades"]
    print(f"Wrote {args.out}")
    print(f"Grades imported: {len(grades)}")
    for g, tmap in grades.items():
        print(f"  {g:28} {len(tmap):2d} thicknesses: {', '.join(tmap.keys())}")
    print(f"\nSkipped (priced on request / unmapped): {len(skipped)} groups")
    for g, n in skipped.items():
        print(f"  {g:40} {n} rows")
    if conflicts:
        print("\nCONFLICTS (same thickness, different price — first kept):")
        for c in conflicts:
            print(f"  {c}")
    if zero_pierce:
        groups = sorted({z.rsplit(" ", 1)[0] for z in zero_pierce})
        print(
            f"\nWARNING: {len(zero_pierce)} priced tariffs have no feed/pierce in the sheet "
            f"-> pierces billed free. Grades: {', '.join(groups)}"
        )


if __name__ == "__main__":
    main()
