"""Geometry helpers shared by the resolver, CRS selection and HUC providers (EPSG:4326)."""

from __future__ import annotations

from pyproj import Geod
from shapely.geometry import MultiPolygon, Polygon
from shapely.geometry.base import BaseGeometry

SQM_PER_SQKM = 1_000_000.0
FULL_CIRCLE_DEG = 360.0
HALF_CIRCLE_DEG = 180.0
_GEOD = Geod(ellps="WGS84")


def polygons(geometry: BaseGeometry) -> list[Polygon]:
    """Polygonal parts of any geometry (drops lines/points left over by intersections)."""
    if isinstance(geometry, Polygon):
        return [] if geometry.is_empty else [geometry]
    if isinstance(geometry, MultiPolygon) or hasattr(geometry, "geoms"):
        return [p for g in geometry.geoms for p in polygons(g)]
    return []


def geodesic_area_sqkm(geometry: BaseGeometry) -> float:
    total = 0.0
    for poly in polygons(geometry):
        area_m2, _ = _GEOD.geometry_area_perimeter(poly)
        total += abs(float(area_m2))
    return total / SQM_PER_SQKM


def close_gaps(geometry: BaseGeometry, epsilon_deg: float) -> BaseGeometry:
    """Morphological closing: fills slivers narrower than ``2 * epsilon_deg``."""
    if epsilon_deg <= 0:
        return geometry
    return geometry.buffer(epsilon_deg).buffer(-epsilon_deg)


def normalize_lon(lon: float) -> float:
    return (lon + HALF_CIRCLE_DEG) % FULL_CIRCLE_DEG - HALF_CIRCLE_DEG


def lon_extent(geometry: BaseGeometry) -> tuple[float, float]:
    """Smallest longitude interval ``(west, east)`` covering the geometry on the circle.

    ``east`` may exceed 180 when the geometry crosses the antimeridian. Works from the
    longitude intervals of the geometry's parts and picks the largest empty gap as the break.
    """
    parts = list(geometry.geoms) if hasattr(geometry, "geoms") else [geometry]
    merged: list[list[float]] = []
    for lo, hi in sorted((p.bounds[0], p.bounds[2]) for p in parts if not p.is_empty):
        if merged and lo <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], hi)
        else:
            merged.append([lo, hi])
    gaps = [(merged[i + 1][0] - merged[i][1], i) for i in range(len(merged) - 1)]
    wrap = merged[0][0] + FULL_CIRCLE_DEG - merged[-1][1]
    best_gap, best_i = max(gaps, default=(-1.0, -1))
    if wrap >= best_gap:
        return merged[0][0], merged[-1][1]
    return merged[best_i + 1][0], merged[best_i][1] + FULL_CIRCLE_DEG
