"""AOI resolver: selector -> union geometry + covering HUC12 set, cached by content hash."""

from __future__ import annotations

import json
from pathlib import Path

from shapely import to_geojson
from shapely.geometry import box
from shapely.ops import unary_union

from hydrology_flow.aoi.crs import recommend_projected_crs
from hydrology_flow.aoi.geometry import close_gaps, geodesic_area_sqkm
from hydrology_flow.aoi.models import AOI, AOISpec, HucUnit, SourceBoundary
from hydrology_flow.aoi.providers import Boundary, BoundaryProvider, HucProvider
from hydrology_flow.telemetry import get_logger, get_tracer

log = get_logger("aoi")
# Bump when resolution logic changes so stale cache entries are ignored.
RESOLVER_VERSION = "2"


class AOIResolver:
    def __init__(
        self,
        boundaries: BoundaryProvider,
        hucs: HucProvider,
        *,
        cache_dir: Path | None = None,
        cache_salt: tuple[str, ...] = (),
        min_overlap_fraction: float = 0.0,
        gap_close_deg: float = 0.0,
    ) -> None:
        self._min_overlap = min_overlap_fraction
        self._gap_close = gap_close_deg
        self._boundaries = boundaries
        self._hucs = hucs
        self._cache_dir = cache_dir
        self._salt = (RESOLVER_VERSION, *cache_salt)

    def _cache_path(self, spec: AOISpec) -> Path | None:
        if self._cache_dir is None:
            return None
        return self._cache_dir / f"{spec.cache_key(*self._salt)}.json"

    async def resolve(self, spec: AOISpec, *, use_cache: bool = True) -> AOI:
        path = self._cache_path(spec)
        if use_cache and path is not None and path.exists():
            try:
                aoi = AOI.model_validate_json(path.read_text())
            except ValueError:
                log.warning("discarding unreadable AOI cache entry", extra={"path": str(path)})
            else:
                log.info("aoi cache hit", extra={"aoi": aoi.name})
                return aoi
        with get_tracer().start_as_current_span("aoi.resolve") as span:
            span.set_attribute("aoi.kind", spec.kind)
            aoi = await self._resolve(spec)
            span.set_attribute("aoi.huc12_count", len(aoi.huc12))
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(aoi.model_dump_json())
            tmp.replace(path)
        return aoi

    async def _resolve(self, spec: AOISpec) -> AOI:
        boundaries: list[Boundary]
        units: list[HucUnit] | None = None
        dropped = 0
        if spec.kind == "bbox":
            assert spec.bbox is not None  # guaranteed by AOISpec.kind
            west, south, east, north = spec.bbox
            boundaries = [
                Boundary(
                    source=SourceBoundary(
                        kind="bbox",
                        name=spec.label(),
                        source_id=",".join(f"{c:g}" for c in (west, south, east, north)),
                        source="user-supplied bounding box",
                        url="",
                    ),
                    geometry=box(west, south, east, north),
                )
            ]
        elif spec.kind == "huc":
            boundaries = await self._hucs.boundaries(spec.huc)
            units = await self._hucs.units_under(spec.huc)
        else:
            boundaries = await self._boundaries.resolve(spec)

        geometry = unary_union([b.geometry for b in boundaries])
        if len(boundaries) > 1:
            geometry = close_gaps(geometry, self._gap_close)
        if units is None:
            cover = await self._hucs.covering(geometry, self._min_overlap)
            units, dropped = cover.units, cover.dropped
        name = _name(spec, boundaries)
        return AOI(
            name=name,
            spec=spec,
            geometry=json.loads(to_geojson(geometry)),
            projected_crs=recommend_projected_crs(geometry),
            bounds=tuple(geometry.bounds),
            area_sqkm=round(geodesic_area_sqkm(geometry), 3),
            sources=[b.source for b in boundaries],
            huc12=units,
            huc12_dropped=dropped,
            huc_min_overlap_fraction=self._min_overlap if spec.kind != "huc" else 0.0,
        )


def _name(spec: AOISpec, boundaries: list[Boundary]) -> str:
    if spec.kind in ("county", "huc") and len(boundaries) > 1:
        return " + ".join(b.source.name for b in boundaries)
    if spec.kind in ("bbox", "zip"):
        return spec.label()
    return boundaries[0].source.name
