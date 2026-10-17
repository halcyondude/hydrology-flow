"""AOI specification (input) and AOI (resolved output) models."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from shapely.geometry import shape
from shapely.geometry.base import BaseGeometry

HUC_MIN_DIGITS = 2
HUC_MAX_DIGITS = 12
HUC12_DIGITS = 12
BBOX_LEN = 4
LON_MIN, LON_MAX = -180.0, 180.0
LAT_MIN, LAT_MAX = -90.0, 90.0
ZIP_DIGITS = 5

AOIKind = Literal["zip", "place", "county", "state", "bbox", "huc"]


class AOISpec(BaseModel):
    """Exactly one selector. ``county`` may hold several counties (their union is the AOI)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    zip: str | None = None
    place: str | None = None
    county: tuple[str, ...] = ()
    state: str | None = None
    bbox: tuple[float, float, float, float] | None = Field(
        default=None, description="west, south, east, north in EPSG:4326"
    )
    huc: tuple[str, ...] = ()

    @field_validator("county", "huc", mode="before")
    @classmethod
    def _listify(cls, v: Any) -> Any:
        return (v,) if isinstance(v, str) else v

    @field_validator("zip")
    @classmethod
    def _zip(cls, v: str | None) -> str | None:
        if v is not None and not (len(v) == ZIP_DIGITS and v.isdigit()):
            raise ValueError("zip must be a 5-digit ZIP code (ZCTA)")
        return v

    @field_validator("huc")
    @classmethod
    def _huc(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        for code in v:
            if not (code.isdigit() and HUC_MIN_DIGITS <= len(code) <= HUC_MAX_DIGITS):
                raise ValueError(f"huc {code!r} must be {HUC_MIN_DIGITS}-{HUC_MAX_DIGITS} digits")
        return v

    @field_validator("bbox")
    @classmethod
    def _bbox(cls, v: tuple[float, float, float, float] | None) -> tuple[float, ...] | None:
        if v is None:
            return v
        west, south, east, north = v
        if not (LON_MIN <= west < east <= LON_MAX and LAT_MIN <= south < north <= LAT_MAX):
            raise ValueError(
                "bbox must be west,south,east,north in EPSG:4326 with west<east, south<north"
            )
        return v

    @model_validator(mode="after")
    def _exactly_one(self) -> Self:
        given = self.kinds
        if len(given) != 1:
            raise ValueError(
                "specify exactly one of zip, place, county (repeatable), state, bbox, huc; "
                f"got {sorted(given) or 'none'}"
            )
        return self

    @property
    def kinds(self) -> list[AOIKind]:
        out: list[AOIKind] = []
        if self.zip:
            out.append("zip")
        if self.place:
            out.append("place")
        if self.county:
            out.append("county")
        if self.state:
            out.append("state")
        if self.bbox:
            out.append("bbox")
        if self.huc:
            out.append("huc")
        return out

    @property
    def kind(self) -> AOIKind:
        return self.kinds[0]

    def label(self) -> str:
        match self.kind:
            case "zip":
                return f"ZIP {self.zip}"
            case "place":
                return str(self.place)
            case "county":
                return " + ".join(self.county)
            case "state":
                return str(self.state)
            case "bbox":
                return "bbox " + ",".join(f"{c:g}" for c in self.bbox or ())
            case "huc":
                return "HUC " + ",".join(self.huc)

    def cache_key(self, *extra: str) -> str:
        payload = json.dumps(self.model_dump(mode="json"), sort_keys=True) + "|" + "|".join(extra)
        return hashlib.sha256(payload.encode()).hexdigest()[:32]


class SourceBoundary(BaseModel):
    """One public boundary that contributed to the AOI, with provenance."""

    kind: AOIKind
    name: str
    source_id: str = Field(description="Identifier in the publishing system (GEOID, HUC code, ...)")
    source: str = Field(description="Publishing service")
    url: str


class HucUnit(BaseModel):
    huc12: str
    name: str
    area_sqkm: float | None = None
    states: str | None = None


class AOI(BaseModel):
    """A resolved area of interest."""

    name: str
    spec: AOISpec
    geometry: dict[str, Any] = Field(description="GeoJSON geometry in EPSG:4326 (union of sources)")
    geometry_crs: str = "EPSG:4326"
    projected_crs: str = Field(description="Recommended projected CRS (EPSG code or PROJ string)")
    bounds: tuple[float, float, float, float]
    area_sqkm: float
    sources: list[SourceBoundary]
    huc12: list[HucUnit]

    @property
    def shape(self) -> BaseGeometry:
        return shape(self.geometry)

    @property
    def huc12_codes(self) -> list[str]:
        return [h.huc12 for h in self.huc12]

    def to_feature(self) -> dict[str, Any]:
        """GeoJSON Feature carrying the AOI geometry plus summary properties."""
        return {
            "type": "Feature",
            "geometry": self.geometry,
            "properties": {
                "name": self.name,
                "projected_crs": self.projected_crs,
                "area_sqkm": self.area_sqkm,
                "sources": [s.model_dump() for s in self.sources],
                "huc12_count": len(self.huc12),
                "huc12": [h.model_dump() for h in self.huc12],
            },
        }
