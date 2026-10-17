"""Live tests against public services. Skipped by default; run with ``pytest -m network``."""

from __future__ import annotations

import pytest

from hydrology_flow.aoi import AOISpec, default_resolver
from hydrology_flow.config import Settings
from hydrology_flow.http import create_client

pytestmark = pytest.mark.network


async def test_live_zip_resolves(settings: Settings) -> None:
    async with create_client(settings) as client:
        aoi = await default_resolver(client, settings).resolve(AOISpec(zip="01602"))
    assert aoi.huc12
    assert aoi.shape.area > 0
    assert all(len(h.huc12) == 12 for h in aoi.huc12)


async def test_live_county_resolves_outside_home_state(settings: Settings) -> None:
    async with create_client(settings) as client:
        aoi = await default_resolver(client, settings).resolve(
            AOISpec(county=("Travis County, TX",))
        )
    assert aoi.huc12
    assert aoi.projected_crs.startswith("EPSG:")
