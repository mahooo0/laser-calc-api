"""Snapshot and unit tests for laser_calc core.

These tests act as a safety net: they pin the current numeric behavior of the
calculation pipeline so any future refactor of the HTTP layer or the package
layout will fail loudly if the math drifts.
"""

from __future__ import annotations

import math
from pathlib import Path

import pytest

from laser_calc import (
    arc_length_from_bulge,
    calculate,
    count_connected_open_paths,
    polygon_area,
)

EXPECTED_CUT_LENGTH_MM = 40.0 + 2.0 * math.pi * 5.0 + 10.0
EXPECTED_AREA_MM2 = 100.0 + math.pi * 25.0
EXPECTED_PIERCES = 3


def test_calculate_baseline(sample_dxf: Path) -> None:
    stats, factor = calculate(str(sample_dxf), tol=0.05, cut_layers=None)

    assert factor == pytest.approx(1.0, rel=1e-9)
    assert stats.cut_length_mm == pytest.approx(EXPECTED_CUT_LENGTH_MM, rel=1e-6)
    assert stats.area_mm2 == pytest.approx(EXPECTED_AREA_MM2, rel=1e-6)
    assert stats.pierces == EXPECTED_PIERCES
    assert stats.unsupported == 0


def test_calculate_layer_filter_excludes_everything(sample_dxf: Path) -> None:
    stats, _ = calculate(str(sample_dxf), tol=0.05, cut_layers={"nonexistent"})

    assert stats.cut_length_mm == 0.0
    assert stats.area_mm2 == 0.0
    assert stats.pierces == 0


def test_polygon_area_square() -> None:
    assert polygon_area([(0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0)]) == pytest.approx(
        100.0
    )


def test_polygon_area_triangle() -> None:
    assert polygon_area([(0.0, 0.0), (10.0, 0.0), (0.0, 10.0)]) == pytest.approx(50.0)


def test_polygon_area_handles_clockwise_winding() -> None:
    assert polygon_area([(0.0, 0.0), (0.0, 10.0), (10.0, 10.0), (10.0, 0.0)]) == pytest.approx(
        100.0
    )


def test_polygon_area_too_few_points() -> None:
    assert polygon_area([(0.0, 0.0), (10.0, 0.0)]) == 0.0


def test_arc_length_zero_bulge_is_chord() -> None:
    assert arc_length_from_bulge((0.0, 0.0), (10.0, 0.0), 0.0) == pytest.approx(10.0)


def test_arc_length_semicircle() -> None:
    # bulge=1 -> 180 deg sweep, chord=10 -> radius=5 -> length=pi*5
    assert arc_length_from_bulge((0.0, 0.0), (10.0, 0.0), 1.0) == pytest.approx(
        math.pi * 5.0, rel=1e-6
    )


def test_arc_length_zero_chord_is_zero() -> None:
    assert arc_length_from_bulge((0.0, 0.0), (0.0, 0.0), 0.5) == 0.0


def test_count_connected_open_paths_empty() -> None:
    assert count_connected_open_paths([], 0.05) == 0


def test_count_connected_open_paths_disjoint() -> None:
    paths = [((0.0, 0.0), (1.0, 0.0)), ((10.0, 0.0), (11.0, 0.0))]
    assert count_connected_open_paths(paths, 0.05) == 2


def test_count_connected_open_paths_chain() -> None:
    # Three segments meeting end-to-end form a single connected component.
    paths = [
        ((0.0, 0.0), (1.0, 0.0)),
        ((1.0, 0.0), (1.0, 1.0)),
        ((1.0, 1.0), (2.0, 1.0)),
    ]
    assert count_connected_open_paths(paths, 0.05) == 1


def test_count_connected_open_paths_snaps_within_tolerance() -> None:
    # Endpoints separated by 0.02 mm should snap together when tol=0.05.
    paths = [
        ((0.0, 0.0), (1.0, 0.0)),
        ((1.02, 0.0), (2.0, 0.0)),
    ]
    assert count_connected_open_paths(paths, 0.05) == 1
