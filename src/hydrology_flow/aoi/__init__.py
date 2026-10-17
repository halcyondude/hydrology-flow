"""AOI resolution: selector (ZIP, place, counties, state, bbox, HUC) -> geometry + HUC12 cover."""

from __future__ import annotations

import httpx

from hydrology_flow.aoi.models import AOI, AOISpec, HucUnit, SourceBoundary
from hydrology_flow.aoi.providers import Boundary, BoundaryProvider, HucProvider
from hydrology_flow.aoi.resolver import AOIResolver
from hydrology_flow.aoi.tigerweb import TigerwebBoundaryProvider
from hydrology_flow.aoi.wbd import WbdProvider
from hydrology_flow.config import Settings


def default_resolver(client: httpx.AsyncClient, settings: Settings) -> AOIResolver:
    """TIGERweb + WBD resolver with results cached (hashed) under the settings cache dir."""
    return AOIResolver(
        TigerwebBoundaryProvider(
            client, simplify_deg=settings.aoi_simplify_deg, page_size=settings.arcgis_page_size
        ),
        WbdProvider(
            client,
            query_simplify_deg=settings.huc_query_simplify_deg,
            boundary_simplify_deg=settings.aoi_simplify_deg,
            page_size=settings.arcgis_page_size,
        ),
        cache_dir=settings.aoi_cache_dir,
        cache_salt=(str(settings.aoi_simplify_deg), str(settings.huc_query_simplify_deg)),
    )


__all__ = [
    "AOI",
    "AOIResolver",
    "AOISpec",
    "Boundary",
    "BoundaryProvider",
    "HucProvider",
    "HucUnit",
    "SourceBoundary",
    "TigerwebBoundaryProvider",
    "WbdProvider",
    "default_resolver",
]
