"""Census TIGERweb REST boundary provider (ZCTA, place, county, state). Public domain."""

from __future__ import annotations

import asyncio
import re
from typing import Any

import httpx
from shapely.geometry import shape
from shapely.geometry.base import BaseGeometry

from hydrology_flow.aoi import arcgis
from hydrology_flow.aoi.models import AOISpec, SourceBoundary
from hydrology_flow.aoi.providers import Boundary

TIGERWEB_BASE = (
    "https://tigerweb.geo.census.gov/arcgis/rest/services/TIGERweb/tigerWMS_Current/MapServer"
)
SOURCE_NAME = "Census TIGERweb (tigerWMS_Current)"
LAYER_ZCTA = 2
LAYER_PLACE = 28
LAYER_CDP = 30
LAYER_COUNTY = 82
LAYER_STATE = 80
# Words the Census appends to county-equivalent names; stripped to fall back to BASENAME.
_COUNTY_SUFFIX = re.compile(
    r"\s+(county|parish|borough|census area|municipality|city and borough)$", re.IGNORECASE
)
_STATE_SPLIT = re.compile(r"^\s*(?P<name>.+?)\s*,\s*(?P<state>[A-Za-z .]+?)\s*$")


class BoundaryNotFoundError(LookupError):
    pass


class TigerwebBoundaryProvider:
    """Resolves political boundaries via TIGERweb. Geometry is returned in EPSG:4326."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        simplify_deg: float,
        page_size: int,
        base_url: str = TIGERWEB_BASE,
    ) -> None:
        self._client = client
        self._simplify = simplify_deg
        self._page_size = page_size
        self._base = base_url

    # ---- low-level -------------------------------------------------------------------

    def _layer_url(self, layer: int) -> str:
        return f"{self._base}/{layer}"

    async def _features(
        self, layer: int, where: str, *, geometry: bool = True, fields: str = "GEOID,NAME,STATE"
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {
            "where": where,
            "outFields": fields,
            "returnGeometry": str(geometry).lower(),
            "outSR": 4326,
            "f": "geojson",
        }
        if geometry and self._simplify > 0:
            params["maxAllowableOffset"] = self._simplify
        return await arcgis.query(
            self._client, self._layer_url(layer), params, page_size=self._page_size
        )

    @staticmethod
    def _geom(feature: dict[str, Any], what: str) -> BaseGeometry:
        if not feature.get("geometry"):
            raise BoundaryNotFoundError(f"{what}: service returned no geometry")
        return shape(feature["geometry"])

    def _boundary(self, kind: str, feature: dict[str, Any], layer: int, what: str) -> Boundary:
        props = feature["properties"]
        source = SourceBoundary(
            kind=kind,
            name=str(props.get("NAME") or props.get("GEOID")),
            source_id=str(props["GEOID"]),
            source=SOURCE_NAME,
            url=self._layer_url(layer),
        )
        return Boundary(source=source, geometry=self._geom(feature, what))

    async def state_fips(self, state: str) -> str:
        """FIPS code from a postal abbreviation or full state name, looked up in TIGERweb."""
        lit = arcgis.sql_literal(state.strip().upper())
        feats = await self._features(
            LAYER_STATE,
            f"UPPER(STUSAB)={lit} OR UPPER(NAME)={lit}",
            geometry=False,
            fields="GEOID,NAME,STUSAB,STATE",
        )
        if not feats:
            raise BoundaryNotFoundError(f"unknown state: {state!r}")
        return str(feats[0]["properties"]["STATE"])

    # ---- selectors -------------------------------------------------------------------

    async def _zip(self, code: str) -> list[Boundary]:
        feats = await self._features(
            LAYER_ZCTA, f"ZCTA5={arcgis.sql_literal(code)}", fields="GEOID,NAME"
        )
        if not feats:
            raise BoundaryNotFoundError(f"ZCTA {code} not found")
        return [self._boundary("zip", feats[0], LAYER_ZCTA, f"ZCTA {code}")]

    async def _place(self, text: str) -> list[Boundary]:
        m = _STATE_SPLIT.match(text)
        if not m:
            raise ValueError(f"place must look like 'City, ST': {text!r}")
        fips = await self.state_fips(m["state"])
        base = arcgis.sql_literal(m["name"].strip().upper())
        where = f"UPPER(BASENAME)={base} AND STATE='{fips}'"
        for layer in (LAYER_PLACE, LAYER_CDP):
            feats = await self._features(layer, where, fields="GEOID,NAME,STATE")
            if len(feats) > 1:
                names = ", ".join(f["properties"]["NAME"] for f in feats)
                raise BoundaryNotFoundError(f"ambiguous place {text!r}: {names}")
            if feats:
                return [self._boundary("place", feats[0], layer, text)]
        raise BoundaryNotFoundError(f"place not found: {text!r}")

    async def _one_county(self, text: str) -> Boundary:
        m = _STATE_SPLIT.match(text)
        if not m:
            raise ValueError(f"county must look like 'Name County, ST': {text!r}")
        fips = await self.state_fips(m["state"])
        full = m["name"].strip()
        stripped = _COUNTY_SUFFIX.sub("", full)
        for field, value in (("NAME", full), ("BASENAME", stripped)):
            where = f"UPPER({field})={arcgis.sql_literal(value.upper())} AND STATE='{fips}'"
            feats = await self._features(LAYER_COUNTY, where, fields="GEOID,NAME,STATE")
            if len(feats) == 1:
                return self._boundary("county", feats[0], LAYER_COUNTY, text)
            if len(feats) > 1:
                names = ", ".join(f["properties"]["NAME"] for f in feats)
                raise BoundaryNotFoundError(f"ambiguous county {text!r}: {names}")
        raise BoundaryNotFoundError(f"county not found: {text!r}")

    async def _counties(self, texts: tuple[str, ...]) -> list[Boundary]:
        return list(await asyncio.gather(*(self._one_county(t) for t in texts)))

    async def _state(self, text: str) -> list[Boundary]:
        fips = await self.state_fips(text)
        feats = await self._features(LAYER_STATE, f"STATE='{fips}'", fields="GEOID,NAME,STATE")
        if not feats:
            raise BoundaryNotFoundError(f"state not found: {text!r}")
        return [self._boundary("state", feats[0], LAYER_STATE, text)]

    async def resolve(self, spec: AOISpec) -> list[Boundary]:
        match spec.kind:
            case "zip":
                return await self._zip(str(spec.zip))
            case "place":
                return await self._place(str(spec.place))
            case "county":
                return await self._counties(spec.county)
            case "state":
                return await self._state(str(spec.state))
            case _:
                raise ValueError(f"TIGERweb cannot resolve selector {spec.kind!r}")
