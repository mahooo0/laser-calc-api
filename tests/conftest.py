"""Shared pytest fixtures for laser_calc tests."""

from __future__ import annotations

from pathlib import Path

import ezdxf
import pytest


@pytest.fixture
def sample_dxf(tmp_path: Path) -> Path:
    """Synthetic DXF with three primitives:

    1. Closed square 10x10 mm at origin -> perimeter 40, area 100, 1 pierce
    2. Circle r=5 mm at (30, 30) -> length 2*pi*5, area pi*25, 1 pierce
    3. Open line from (50, 0) to (60, 0) -> length 10, area 0, 1 pierce (via open-path stitching)

    Total: cut_length_mm = 50 + 10*pi, area_mm2 = 100 + 25*pi, pierces = 3.
    """
    doc = ezdxf.new(setup=True)
    doc.units = 4  # ezdxf.units.MM
    msp = doc.modelspace()

    msp.add_lwpolyline(
        [(0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0)],
        close=True,
    )
    msp.add_circle((30.0, 30.0), 5.0)
    msp.add_line((50.0, 0.0), (60.0, 0.0))

    path = tmp_path / "sample.dxf"
    doc.saveas(path)
    return path
