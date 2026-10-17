"""USGS Watershed Boundary Dataset (WBD) provider via the National Map MapServer."""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

import httpx
from shapely.geometry import shape
from shapely.geometry.base import BaseGeometry

from hydrology_flow.aoi import arcgis
from hydrology_flow.aoi.models import (
    HUC12_DIGITS,
    HUC_MAX_DIGITS,
    HUC_MIN_DIGITS,
    HucUnit,
    SourceBoundary,
)
from hydrology_flow.aoi.providers import Boundary

WBD_BASE = "https://hydro.nationalmap.gov/arcgis/rest/services/wbd/MapServer"
SOURCE_NAME = "USGS Watershed Boundary Dataset (National Map MapServer)"
# MapServer layer id by HUC digit count (2-digit region ... 12-digit subwatershed).
LAYER_BY_DIGITS = {2: 1, 4: 2, 6: 3, 8: 4, 10: 5, 12: 6}
HUC_FIELDS = "huc12,name,areasqkm,states"


class WbdProvider:
    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        query_simplify_deg: float,
        boundary_simplify_deg: float,
        page_size: int,
        base_url: str = WBD_BASE,
    ) -> None:
        self._client = client
        self._query_simplify = query_simplify_deg
        self._boundary_simplify = boundary_simplify_deg
        self._page_size = page_size
        self._base = base_url

    def _layer_url(self, digits: int) -> str:
        return f"{self._base}/{LAYER_BY_DIGITS[digits]}"

    @staticmethod
    def _unit(attrs: dict[str, Any]) -> HucUnit:
        return HucUnit(
            huc12=str(attrs["huc12"]),
            name=str(attrs.get("name") or ""),
            area_sqkm=attrs.get("areasqkm"),
            states=attrs.get("states"),
        )

    async def covering(self, geometry: BaseGeometry) -> list[HucUnit]:
        query_geom = geometry.simplify(self._query_simplify, preserve_topology=True)
        params = {
            "geometry": json.dumps(arcgis.esri_polygon_json(query_geom)),
            "geometryType": "esriGeometryPolygon",
            "spatialRel": "esriSpatialRelIntersects",
            "inSR": 4326,
            "where": "1=1",
            "outFields": HUC_FIELDS,
            "returnGeometry": "false",
            "orderByFields": "huc12",
            "f": "json",
        }
        feats = await arcgis.query(
            self._client, self._layer_url(HUC12_DIGITS), params, page_size=self._page_size
        )
        return sorted(
            {u.huc12: u for u in (self._unit(f["attributes"]) for f in feats)}.values(),
            key=lambda u: u.huc12,
        )

    async def units_under(self, codes: Sequence[str]) -> list[HucUnit]:
        found: dict[str, HucUnit] = {}
        for code in codes:
            _check(code)
            params = {
                "where": f"huc12 LIKE '{code}%'",
                "outFields": HUC_FIELDS,
                "returnGeometry": "false",
                "orderByFields": "huc12",
                "f": "json",
            }
            feats = await arcgis.query(
                self._client, self._layer_url(HUC12_DIGITS), params, page_size=self._page_size
            )
            for f in feats:
                unit = self._unit(f["attributes"])
                found[unit.huc12] = unit
        return sorted(found.values(), key=lambda u: u.huc12)

    async def boundaries(self, codes: Sequence[str]) -> list[Boundary]:
        out: list[Boundary] = []
        for code in codes:
            _check(code)
            digits = len(code)
            field = f"huc{digits}"
            params: dict[str, Any] = {
                "where": f"{field}={arcgis.sql_literal(code)}",
                "outFields": f"{field},name",
                "returnGeometry": "true",
                "outSR": 4326,
                "f": "geojson",
            }
            if self._boundary_simplify > 0:
                params["maxAllowableOffset"] = self._boundary_simplify
            feats = await arcgis.query(
                self._client, self._layer_url(digits), params, page_size=self._page_size
            )
            if not feats or not feats[0].get("geometry"):
                raise LookupError(f"HUC {code} not found in WBD")
            props = feats[0]["properties"]
            out.append(
                Boundary(
                    source=SourceBoundary(
                        kind="huc",
                        name=str(props.get("name") or code),
                        source_id=code,
                        source=SOURCE_NAME,
                        url=self._layer_url(digits),
                    ),
                    geometry=shape(feats[0]["geometry"]),
                )
            )
        return out


def _check(code: str) -> None:
    if not (
        code.isdigit() and HUC_MIN_DIGITS <= len(code) <= HUC_MAX_DIGITS and len(code) % 2 == 0
    ):
        raise ValueError(f"HUC code must be an even number of digits, 2-12: {code!r}")
