"""Minimal ArcGIS REST ``query`` helper (paginated), built on the shared HTTP client."""

from __future__ import annotations

import re
from typing import Any

import httpx
from shapely.geometry import MultiPolygon, Polygon, mapping
from shapely.geometry.base import BaseGeometry
from shapely.geometry.polygon import orient

# Values interpolated into SQL where-clauses are restricted to this alphabet.
_SAFE_VALUE = re.compile(r"^[\w .,'()&/-]+$")
CLOCKWISE = -1.0


class ArcGISError(RuntimeError):
    pass


def sql_literal(value: str) -> str:
    """Quote a user-supplied string for an ArcGIS where-clause (rejects odd characters)."""
    if not _SAFE_VALUE.match(value):
        raise ValueError(f"unsupported characters in query value: {value!r}")
    return "'" + value.replace("'", "''") + "'"


def esri_polygon_json(geometry: BaseGeometry) -> dict[str, Any]:
    """Esri JSON for a (Multi)Polygon in EPSG:4326 (exteriors clockwise, holes counter-clockwise)."""
    polys: list[Polygon]
    if isinstance(geometry, Polygon):
        polys = [geometry]
    elif isinstance(geometry, MultiPolygon):
        polys = list(geometry.geoms)
    else:
        raise TypeError(f"expected polygonal geometry, got {geometry.geom_type}")
    rings: list[list[list[float]]] = []
    for poly in polys:
        oriented = orient(poly, sign=CLOCKWISE)
        rings.append([list(c) for c in oriented.exterior.coords])
        rings.extend([list(c) for c in interior.coords] for interior in oriented.interiors)
    return {"rings": rings, "spatialReference": {"wkid": 4326}}


async def query(
    client: httpx.AsyncClient,
    layer_url: str,
    params: dict[str, Any],
    *,
    page_size: int,
) -> list[dict[str, Any]]:
    """POST ``{layer_url}/query`` and follow ``resultOffset`` paging. Returns raw features.

    ``f`` must be ``geojson`` or ``json``; the feature list is returned as served.
    """
    out: list[dict[str, Any]] = []
    offset = 0
    while True:
        body = {
            **params,
            "resultOffset": offset,
            "resultRecordCount": page_size,
        }
        resp = await client.post(f"{layer_url}/query", data=body)
        resp.raise_for_status()
        payload = resp.json()
        if "error" in payload:
            raise ArcGISError(f"{layer_url}: {payload['error']}")
        features = payload.get("features", [])
        out.extend(features)
        exceeded = payload.get("exceededTransferLimit") or payload.get("properties", {}).get(
            "exceededTransferLimit"
        )
        if not features or not exceeded:
            return out
        offset += len(features)


def geojson_geometry(geometry: BaseGeometry) -> dict[str, Any]:
    return dict(mapping(geometry))
