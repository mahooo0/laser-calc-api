#!/usr/bin/env python3
"""
DXF laser cutting calculator (MVP).

Reads DXF with ezdxf recover mode, calculates:
- cut length
- pierce count
- approximate part area
- total price
"""

from __future__ import annotations

import argparse
import math
import sys
from collections import defaultdict
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

import ezdxf
from ezdxf import recover, units

Point2D = Tuple[float, float]


@dataclass
class CalcStats:
    cut_length_mm: float = 0.0
    area_mm2: float = 0.0
    pierces: int = 0
    unsupported: int = 0
    warnings: int = 0


def dist(a: Point2D, b: Point2D) -> float:
    return math.hypot(b[0] - a[0], b[1] - a[1])


def polygon_area(points: Sequence[Point2D]) -> float:
    if len(points) < 3:
        return 0.0
    acc = 0.0
    for i in range(len(points)):
        x1, y1 = points[i]
        x2, y2 = points[(i + 1) % len(points)]
        acc += x1 * y2 - x2 * y1
    return abs(acc) * 0.5


def arc_length_from_bulge(p0: Point2D, p1: Point2D, bulge: float) -> float:
    chord = dist(p0, p1)
    if chord == 0.0:
        return 0.0
    if abs(bulge) < 1e-12:
        return chord
    theta = 4.0 * math.atan(bulge)
    s = math.sin(theta / 2.0)
    if abs(s) < 1e-12:
        return chord
    radius = chord / (2.0 * s)
    return abs(radius * theta)


def arc_points_from_bulge(
    p0: Point2D, p1: Point2D, bulge: float, tol: float
) -> List[Point2D]:
    if abs(bulge) < 1e-12 or dist(p0, p1) == 0.0:
        return [p0, p1]

    theta = 4.0 * math.atan(bulge)
    chord = dist(p0, p1)
    s = math.sin(theta / 2.0)
    if abs(s) < 1e-12:
        return [p0, p1]

    radius = chord / (2.0 * s)
    mx = (p0[0] + p1[0]) * 0.5
    my = (p0[1] + p1[1]) * 0.5
    vx = p1[0] - p0[0]
    vy = p1[1] - p0[1]
    vlen = math.hypot(vx, vy)
    if vlen == 0:
        return [p0, p1]

    ux = vx / vlen
    uy = vy / vlen
    px = -uy
    py = ux

    tan_half = math.tan(theta / 2.0)
    if abs(tan_half) < 1e-12:
        return [p0, p1]
    h = chord / (2.0 * tan_half)
    cx = mx + px * h
    cy = my + py * h

    start = math.atan2(p0[1] - cy, p0[0] - cx)
    sweep = theta

    r_abs = abs(radius)
    if r_abs < 1e-9:
        return [p0, p1]
    tol = max(tol, 1e-6)
    max_angle = 2.0 * math.acos(max(-1.0, min(1.0, 1.0 - tol / r_abs)))
    if max_angle <= 0.0 or math.isnan(max_angle):
        max_angle = math.radians(10.0)
    steps = max(4, int(math.ceil(abs(sweep) / max_angle)))

    pts = []
    for i in range(steps + 1):
        a = start + sweep * (i / steps)
        pts.append((cx + r_abs * math.cos(a), cy + r_abs * math.sin(a)))
    return pts


def lwpolyline_points(entity, tol: float) -> List[Point2D]:
    raw = list(entity.get_points("xyb"))
    n = len(raw)
    if n == 0:
        return []
    out: List[Point2D] = []
    limit = n if entity.closed else n - 1
    for i in range(max(limit, 0)):
        x0, y0, b = raw[i]
        x1, y1, _ = raw[(i + 1) % n]
        seg_pts = arc_points_from_bulge((x0, y0), (x1, y1), b, tol)
        if out:
            out.extend(seg_pts[1:])
        else:
            out.extend(seg_pts)
    if not entity.closed and n > 0 and (not out or out[-1] != (raw[-1][0], raw[-1][1])):
        out.append((raw[-1][0], raw[-1][1]))
    return out


def polyline_points(entity, tol: float) -> List[Point2D]:
    verts = list(entity.vertices)
    n = len(verts)
    if n == 0:
        return []
    pts = [
        ((v.dxf.location.x), (v.dxf.location.y), float(getattr(v.dxf, "bulge", 0.0)))
        for v in verts
    ]
    out: List[Point2D] = []
    limit = n if entity.is_closed else n - 1
    for i in range(max(limit, 0)):
        x0, y0, b = pts[i]
        x1, y1, _ = pts[(i + 1) % n]
        seg_pts = arc_points_from_bulge((x0, y0), (x1, y1), b, tol)
        if out:
            out.extend(seg_pts[1:])
        else:
            out.extend(seg_pts)
    if not entity.is_closed and n > 0 and (not out or out[-1] != (pts[-1][0], pts[-1][1])):
        out.append((pts[-1][0], pts[-1][1]))
    return out


def flatten_entity_points(entity, tol: float) -> List[Point2D]:
    t = entity.dxftype()
    if t == "LWPOLYLINE":
        return lwpolyline_points(entity, tol)
    if t == "POLYLINE" and entity.is_2d_polyline:
        return polyline_points(entity, tol)
    if t in {"SPLINE", "ELLIPSE"}:
        return [(p.x, p.y) for p in entity.flattening(distance=tol)]
    if t == "CIRCLE":
        r = float(entity.dxf.radius)
        c = entity.dxf.center
        if r <= 0:
            return []
        steps = max(24, int(math.ceil(2.0 * math.pi * r / max(tol, 0.1))))
        out = []
        for i in range(steps):
            a = (2.0 * math.pi * i) / steps
            out.append((c.x + r * math.cos(a), c.y + r * math.sin(a)))
        return out
    if t == "ARC":
        c = entity.dxf.center
        r = float(entity.dxf.radius)
        if r <= 0:
            return []
        a0 = math.radians(float(entity.dxf.start_angle))
        a1 = math.radians(float(entity.dxf.end_angle))
        if a1 < a0:
            a1 += 2.0 * math.pi
        sweep = a1 - a0
        steps = max(6, int(math.ceil((sweep * r) / max(tol, 0.1))))
        return [
            (c.x + r * math.cos(a0 + sweep * i / steps), c.y + r * math.sin(a0 + sweep * i / steps))
            for i in range(steps + 1)
        ]
    if t == "LINE":
        s = entity.dxf.start
        e = entity.dxf.end
        return [(s.x, s.y), (e.x, e.y)]
    return []


def iter_flat_entities(entity, stats: CalcStats):
    if entity.dxftype() == "INSERT":
        try:
            for child in entity.virtual_entities():
                yield from iter_flat_entities(child, stats)
        except Exception:
            stats.warnings += 1
        return
    yield entity


def entity_cut_length(entity) -> float:
    t = entity.dxftype()
    if t == "LINE":
        return dist((entity.dxf.start.x, entity.dxf.start.y), (entity.dxf.end.x, entity.dxf.end.y))
    if t == "CIRCLE":
        return 2.0 * math.pi * float(entity.dxf.radius)
    if t == "ARC":
        r = float(entity.dxf.radius)
        a0 = math.radians(float(entity.dxf.start_angle))
        a1 = math.radians(float(entity.dxf.end_angle))
        if a1 < a0:
            a1 += 2.0 * math.pi
        return abs(a1 - a0) * r
    if t == "LWPOLYLINE":
        pts = list(entity.get_points("xyb"))
        n = len(pts)
        if n < 2:
            return 0.0
        total = 0.0
        limit = n if entity.closed else n - 1
        for i in range(limit):
            p0 = (pts[i][0], pts[i][1])
            p1 = (pts[(i + 1) % n][0], pts[(i + 1) % n][1])
            total += arc_length_from_bulge(p0, p1, pts[i][2])
        return total
    if t == "POLYLINE" and entity.is_2d_polyline:
        verts = list(entity.vertices)
        n = len(verts)
        if n < 2:
            return 0.0
        total = 0.0
        limit = n if entity.is_closed else n - 1
        for i in range(limit):
            v0 = verts[i]
            v1 = verts[(i + 1) % n]
            p0 = (v0.dxf.location.x, v0.dxf.location.y)
            p1 = (v1.dxf.location.x, v1.dxf.location.y)
            bulge = float(getattr(v0.dxf, "bulge", 0.0))
            total += arc_length_from_bulge(p0, p1, bulge)
        return total
    if t in {"SPLINE", "ELLIPSE"}:
        pts = flatten_entity_points(entity, tol=0.05)
        if len(pts) < 2:
            return 0.0
        return sum(dist(pts[i], pts[i + 1]) for i in range(len(pts) - 1))
    return 0.0


def entity_area(entity, tol: float) -> float:
    t = entity.dxftype()
    if t == "CIRCLE":
        r = float(entity.dxf.radius)
        return math.pi * r * r
    if t == "ELLIPSE":
        start = float(entity.dxf.start_param)
        end = float(entity.dxf.end_param)
        sweep = end - start
        if sweep < 0.0:
            sweep += 2.0 * math.pi
        if abs(sweep - 2.0 * math.pi) < 1e-6:
            a = entity.dxf.major_axis.magnitude
            b = a * float(entity.dxf.ratio)
            return math.pi * a * b
        return 0.0
    if t == "LWPOLYLINE" and entity.closed:
        return polygon_area(lwpolyline_points(entity, tol))
    if t == "POLYLINE" and entity.is_2d_polyline and entity.is_closed:
        return polygon_area(polyline_points(entity, tol))
    if t == "SPLINE" and bool(getattr(entity, "closed", False)):
        pts = flatten_entity_points(entity, tol=tol)
        return polygon_area(pts)
    return 0.0


def is_supported(entity) -> bool:
    t = entity.dxftype()
    if t in {"LINE", "CIRCLE", "ARC", "LWPOLYLINE", "ELLIPSE", "SPLINE"}:
        return True
    if t == "POLYLINE" and entity.is_2d_polyline:
        return True
    if t == "INSERT":
        return True
    return False


def load_doc(path: str):
    try:
        doc, auditor = recover.readfile(path)
        if len(auditor.errors) > 0:
            print(f"[warn] DXF recovered with {len(auditor.errors)} issues")
        return doc
    except Exception:
        return ezdxf.readfile(path)


def unit_to_mm_factor(doc) -> float:
    try:
        du = int(doc.units)
    except Exception:
        du = 0
    if du == 0:
        return 1.0
    try:
        return float(units.conversion_factor(du, units.MM))
    except Exception:
        return 1.0


def normalize_layers(values: Optional[Iterable[str]]) -> Optional[set]:
    if not values:
        return None
    out = set()
    for item in values:
        for part in item.split(","):
            name = part.strip()
            if name:
                out.add(name.lower())
    return out if out else None


def entity_is_closed(entity, points: Sequence[Point2D], tol: float) -> bool:
    t = entity.dxftype()
    if t == "CIRCLE":
        return True
    if t == "LWPOLYLINE":
        return bool(entity.closed)
    if t == "POLYLINE" and entity.is_2d_polyline:
        return bool(entity.is_closed)
    if t == "SPLINE":
        if bool(getattr(entity, "closed", False)):
            return True
    if t == "ELLIPSE":
        try:
            start = float(entity.dxf.start_param)
            end = float(entity.dxf.end_param)
            sweep = end - start
            if sweep < 0.0:
                sweep += 2.0 * math.pi
            if abs(sweep - 2.0 * math.pi) < 1e-6:
                return True
        except Exception:
            pass
    if len(points) >= 2 and dist(points[0], points[-1]) <= tol:
        return True
    return False


def _find(parent: List[int], i: int) -> int:
    while parent[i] != i:
        parent[i] = parent[parent[i]]
        i = parent[i]
    return i


def _union(parent: List[int], a: int, b: int) -> None:
    ra = _find(parent, a)
    rb = _find(parent, b)
    if ra != rb:
        parent[rb] = ra


def _snap_node(
    p: Point2D,
    tol: float,
    nodes: List[Point2D],
    grid: Dict[Tuple[int, int], List[int]],
) -> int:
    cell_size = max(tol, 1e-9)
    cx = int(round(p[0] / cell_size))
    cy = int(round(p[1] / cell_size))
    for ix in range(cx - 1, cx + 2):
        for iy in range(cy - 1, cy + 2):
            for node_idx in grid.get((ix, iy), []):
                if dist(nodes[node_idx], p) <= tol:
                    return node_idx

    node_idx = len(nodes)
    nodes.append(p)
    grid.setdefault((cx, cy), []).append(node_idx)
    return node_idx


def count_connected_open_paths(
    open_paths: Sequence[Tuple[Point2D, Point2D]],
    join_tol: float,
) -> int:
    if not open_paths:
        return 0

    parent = list(range(len(open_paths)))
    nodes: List[Point2D] = []
    grid: Dict[Tuple[int, int], List[int]] = {}
    node_to_edges: Dict[int, List[int]] = defaultdict(list)

    for edge_idx, (start, end) in enumerate(open_paths):
        n0 = _snap_node(start, join_tol, nodes, grid)
        n1 = _snap_node(end, join_tol, nodes, grid)
        node_to_edges[n0].append(edge_idx)
        node_to_edges[n1].append(edge_idx)

    for edges in node_to_edges.values():
        if len(edges) < 2:
            continue
        first = edges[0]
        for other in edges[1:]:
            _union(parent, first, other)

    roots = {_find(parent, i) for i in range(len(open_paths))}
    return len(roots)


def calculate(
    file_path: str,
    tol: float,
    cut_layers: Optional[set],
) -> Tuple[CalcStats, float]:
    doc = load_doc(file_path)
    msp = doc.modelspace()
    factor = unit_to_mm_factor(doc)

    stats = CalcStats()
    closed_pierces = 0
    open_paths: List[Tuple[Point2D, Point2D]] = []
    join_tol = max(tol, 0.05)

    for e in msp:
        for ent in iter_flat_entities(e, stats):
            layer = str(getattr(ent.dxf, "layer", "0")).lower()
            if cut_layers and layer not in cut_layers:
                continue
            if not is_supported(ent):
                stats.unsupported += 1
                continue

            length = entity_cut_length(ent)
            area = entity_area(ent, tol)
            if length <= 0.0 and area <= 0.0 and ent.dxftype() not in {"SPLINE", "ELLIPSE"}:
                continue

            stats.cut_length_mm += length * factor
            stats.area_mm2 += area * (factor * factor)

            if length > 0.0:
                points = flatten_entity_points(ent, tol)
                if len(points) >= 2:
                    if entity_is_closed(ent, points, join_tol):
                        closed_pierces += 1
                    else:
                        open_paths.append((points[0], points[-1]))

    stats.pierces = closed_pierces + count_connected_open_paths(open_paths, join_tol)
    return stats, factor


def main() -> int:
    p = argparse.ArgumentParser(description="DXF laser cutting calculator")
    p.add_argument("file", help="Path to DXF file")
    p.add_argument("--tol", type=float, default=0.05, help="Curve approximation tolerance in mm")
    p.add_argument("--cut-layers", nargs="*", help="Only these layers (space/comma separated)")
    p.add_argument("--price-meter", type=float, default=0.0, help="Cut price per meter")
    p.add_argument("--price-pierce", type=float, default=0.0, help="Pierce price per unit")
    p.add_argument("--material-m2", type=float, default=0.0, help="Material price per m^2")
    args = p.parse_args()

    try:
        cut_layers = normalize_layers(args.cut_layers)
        stats, factor = calculate(args.file, tol=max(args.tol, 0.001), cut_layers=cut_layers)
    except FileNotFoundError:
        print(f"[error] File not found: {args.file}")
        return 2
    except ezdxf.DXFError as ex:
        print(f"[error] Invalid DXF: {ex}")
        return 3
    except Exception as ex:
        print(f"[error] Failed: {ex}")
        return 4

    cut_m = stats.cut_length_mm / 1000.0
    area_m2 = stats.area_mm2 / 1_000_000.0
    total = (
        cut_m * args.price_meter
        + stats.pierces * args.price_pierce
        + area_m2 * args.material_m2
    )

    print("=== DXF Laser Calc ===")
    print(f"Units to mm factor: {factor:.6g}")
    print(f"Cut length:         {stats.cut_length_mm:.3f} mm ({cut_m:.3f} m)")
    print(f"Pierces:            {stats.pierces}")
    print(f"Approx area:        {stats.area_mm2:.3f} mm^2 ({area_m2:.6f} m^2)")
    print(f"Unsupported ents:   {stats.unsupported}")
    print(f"Warnings:           {stats.warnings}")
    print("---")
    print(f"Price total:        {total:.2f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
