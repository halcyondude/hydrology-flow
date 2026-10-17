"""Live tests against public services. Skipped by default; run with ``pytest -m network``.

Expectations are anchored on codes resolved live from the same services (never typed in), so
they hold across WBD refreshes while still asserting real, known units.
"""

from __future__ import annotations

import httpx
import pytest

from hydrology_flow.aoi import AOI, AOISpec, arcgis, default_resolver
from hydrology_flow.aoi.geometry import lon_extent
from hydrology_flow.aoi.wbd import WBD_BASE
from hydrology_flow.config import Settings
from hydrology_flow.http import create_client

pytestmark = pytest.mark.network

HUC12_LAYER = f"{WBD_BASE}/6"


async def resolve(settings: Settings, spec: AOISpec) -> AOI:
    async with create_client(settings) as client:
        return await default_resolver(client, settings).resolve(spec)


async def huc12_codes_named(
    client: httpx.AsyncClient, name: str, state: str, prefix: str
) -> set[str]:
    feats = await arcgis.query(
        client,
        HUC12_LAYER,
        {
            "where": f"name={arcgis.sql_literal(name)} AND states LIKE '%{state}%' AND huc12 LIKE '{prefix}%'",
            "outFields": "huc12",
            "returnGeometry": "false",
            "f": "json",
        },
        page_size=100,
    )
    return {f["attributes"]["huc12"] for f in feats}


async def test_zip_includes_named_huc12(settings: Settings) -> None:
    aoi = await resolve(settings, AOISpec(zip="01602"))
    async with create_client(settings) as client:
        expected = await huc12_codes_named(client, "Tatnuck Brook-Blackstone River", "MA", "0109")
    assert len(expected) == 1  # resolved live, not typed in
    assert expected <= set(aoi.huc12_codes)
    assert all(len(c) == 12 for c in aoi.huc12_codes)
    assert aoi.shape.area > 0


async def test_county_outside_home_state_resolves_to_its_own_basins(settings: Settings) -> None:
    aoi = await resolve(settings, AOISpec(county=("Travis County, TX",)))
    assert aoi.huc12
    assert aoi.projected_crs.startswith("EPSG:")
    assert {c[:2] for c in aoi.huc12_codes} == {"12"}  # Texas-Gulf region


async def test_adjacent_counties_union_has_no_sliver_holes(settings: Settings) -> None:
    aoi = await resolve(settings, AOISpec(county=("King County, WA", "Pierce County, WA")))
    parts = list(aoi.shape.geoms) if hasattr(aoi.shape, "geoms") else [aoi.shape]
    assert sum(len(p.interiors) for p in parts) == 0
    assert len(aoi.sources) == 2


async def test_overlap_threshold_drops_grazing_huc12s(settings: Settings) -> None:
    strict = settings.model_copy(update={"huc_min_overlap_fraction": 0.01})
    off = settings.model_copy(update={"huc_min_overlap_fraction": 0.0})
    spec = AOISpec(county=("King County, WA", "Pierce County, WA"))
    with_threshold = await resolve(strict, spec)
    without = await resolve(off, spec)
    assert with_threshold.huc12_dropped > 0
    assert len(without.huc12) == len(with_threshold.huc12) + with_threshold.huc12_dropped
    assert set(with_threshold.huc12_codes) <= set(without.huc12_codes)


async def test_antimeridian_county_gets_a_centred_crs(settings: Settings) -> None:
    aoi = await resolve(settings, AOISpec(county=("Aleutians West Census Area, AK",)))
    west, east = lon_extent(aoi.shape)
    assert east > 180 > west  # crosses the antimeridian
    assert aoi.projected_crs.startswith("+proj=laea")
    lon_0 = float(aoi.projected_crs.split("+lon_0=")[1].split()[0])
    assert abs(lon_0) > 170  # near the antimeridian, not near the naive bounds centre
