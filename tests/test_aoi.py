from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from pydantic import ValidationError
from shapely.geometry import MultiPolygon, Polygon, box, shape

from hydrology_flow.aoi import (
    AOIResolver,
    AOISpec,
    BoundaryProvider,
    HucProvider,
    TigerwebBoundaryProvider,
    WbdProvider,
)
from hydrology_flow.aoi.arcgis import esri_polygon_json, sql_literal
from hydrology_flow.aoi.crs import recommend_projected_crs
from hydrology_flow.aoi.tigerweb import TIGERWEB_BASE, BoundaryNotFoundError
from hydrology_flow.aoi.wbd import WBD_BASE
from tests.conftest import FakeBoundaries, FakeHucs

SQUARE = {"type": "Polygon", "coordinates": [[[1, 2], [1, 3], [2, 3], [2, 2], [1, 2]]]}


# ---- spec validation ---------------------------------------------------------------


def test_spec_requires_exactly_one_selector() -> None:
    with pytest.raises(ValidationError):
        AOISpec()
    with pytest.raises(ValidationError):
        AOISpec(zip="12345", state="ST")


def test_spec_county_is_repeatable_and_coerced() -> None:
    assert AOISpec(county="A County, ST").county == ("A County, ST",)
    assert len(AOISpec(county=("A County, ST", "B County, ST")).county) == 2


@pytest.mark.parametrize("bad", ["1234", "abcde", "123456"])
def test_spec_rejects_bad_zip(bad: str) -> None:
    with pytest.raises(ValidationError):
        AOISpec(zip=bad)


def test_spec_rejects_bad_huc_and_bbox() -> None:
    with pytest.raises(ValidationError):
        AOISpec(huc=("1",))
    with pytest.raises(ValidationError):
        AOISpec(bbox=(10.0, 0.0, 5.0, 1.0))
    with pytest.raises(ValidationError):
        AOISpec(bbox=(0.0, 0.0, 1.0, 95.0))


def test_spec_cache_key_is_stable_and_selector_sensitive() -> None:
    assert AOISpec(zip="12345").cache_key() == AOISpec(zip="12345").cache_key()
    assert AOISpec(zip="12345").cache_key() != AOISpec(zip="12346").cache_key()
    assert AOISpec(zip="12345").cache_key("v2") != AOISpec(zip="12345").cache_key()


# ---- helpers -----------------------------------------------------------------------


def test_sql_literal_quotes_and_rejects() -> None:
    assert sql_literal("O'BRIEN") == "'O''BRIEN'"
    with pytest.raises(ValueError):
        sql_literal("x; DROP TABLE y")


def test_esri_rings_have_clockwise_exteriors_and_ccw_holes() -> None:
    donut = Polygon(box(0, 0, 10, 10).exterior.coords, [box(2, 2, 4, 4).exterior.coords])
    rings = esri_polygon_json(MultiPolygon([donut]))["rings"]
    assert len(rings) == 2
    assert Polygon(rings[0]).exterior.is_ccw is False
    assert Polygon(rings[1]).exterior.is_ccw is True


def test_crs_single_zone_vs_wide() -> None:
    assert recommend_projected_crs(box(-71.9, 42.2, -71.8, 42.3)) == "EPSG:32619"
    assert recommend_projected_crs(box(-35.0, -10.0, -34.0, -9.0)).startswith("EPSG:327")
    assert recommend_projected_crs(box(-100.0, 30.0, -90.0, 35.0)).startswith("+proj=laea")


# ---- resolver with fake providers --------------------------------------------------


def test_fakes_satisfy_protocols() -> None:
    assert isinstance(FakeBoundaries(), BoundaryProvider)
    assert isinstance(FakeHucs(), HucProvider)


async def test_resolver_builds_aoi_and_caches(tmp_path: Path) -> None:
    boundaries, hucs = FakeBoundaries(), FakeHucs()
    resolver = AOIResolver(boundaries, hucs, cache_dir=tmp_path)
    spec = AOISpec(state="ST")
    aoi = await resolver.resolve(spec)
    assert aoi.name == "Fake Place"
    assert aoi.huc12_codes == ["010100000101", "010100000102"]
    assert aoi.projected_crs.startswith("EPSG:326")
    assert aoi.area_sqkm > 0
    assert shape(aoi.geometry).bounds == (10.0, 20.0, 11.0, 21.0)

    again = await resolver.resolve(spec)
    assert again == aoi
    assert boundaries.calls == 1
    assert hucs.calls == 1
    assert len(list(tmp_path.glob("*.json"))) == 1

    await resolver.resolve(spec, use_cache=False)
    assert boundaries.calls == 2


async def test_resolver_bbox_skips_boundary_provider() -> None:
    boundaries = FakeBoundaries()
    aoi = await AOIResolver(boundaries, FakeHucs()).resolve(AOISpec(bbox=(1.0, 2.0, 3.0, 4.0)))
    assert boundaries.calls == 0
    assert aoi.bounds == (1.0, 2.0, 3.0, 4.0)
    assert aoi.sources[0].kind == "bbox"


async def test_resolver_huc_uses_units_under() -> None:
    aoi = await AOIResolver(FakeBoundaries(), FakeHucs()).resolve(AOISpec(huc=("0101",)))
    assert aoi.huc12_codes == ["010100000000"]
    assert aoi.sources[0].kind == "huc"


async def test_resolver_unions_multiple_counties() -> None:
    class TwoCounties(FakeBoundaries):
        async def resolve(self, spec: AOISpec) -> list[Any]:
            first = (await super().resolve(spec))[0]
            second = type(first)(
                source=first.source.model_copy(update={"name": "Other", "source_id": "2"}),
                geometry=box(11.0, 20.0, 12.0, 21.0),
            )
            return [first, second]

    aoi = await AOIResolver(TwoCounties(), FakeHucs()).resolve(AOISpec(county=("A, ST", "B, ST")))
    assert aoi.name == "Fake Place + Other"
    assert aoi.bounds == (10.0, 20.0, 12.0, 21.0)
    assert len(aoi.sources) == 2


def test_to_feature_roundtrip() -> None:
    import asyncio

    aoi = asyncio.run(AOIResolver(FakeBoundaries(), FakeHucs()).resolve(AOISpec(state="ST")))
    feature = aoi.to_feature()
    assert feature["type"] == "Feature"
    assert feature["properties"]["huc12_count"] == 2
    json.dumps(feature)


# ---- TIGERweb provider over mocked HTTP --------------------------------------------


def tiger(client: httpx.AsyncClient) -> TigerwebBoundaryProvider:
    return TigerwebBoundaryProvider(client, simplify_deg=0.0, page_size=100)


def feat(props: dict[str, Any], geom: dict[str, Any] | None = SQUARE) -> dict[str, Any]:
    return {"type": "Feature", "geometry": geom, "properties": props}


def fc(*features: dict[str, Any]) -> httpx.Response:
    return httpx.Response(200, json={"type": "FeatureCollection", "features": list(features)})


def where_of(request: httpx.Request) -> str:
    from urllib.parse import parse_qs

    return parse_qs(request.content.decode())["where"][0]


@respx.mock
async def test_tigerweb_zip() -> None:
    route = respx.post(f"{TIGERWEB_BASE}/2/query").mock(
        return_value=fc(feat({"GEOID": "12345", "NAME": "ZCTA5 12345"}))
    )
    async with httpx.AsyncClient() as client:
        (b,) = await tiger(client).resolve(AOISpec(zip="12345"))
    assert "ZCTA5='12345'" in where_of(route.calls.last.request)
    assert b.source.source_id == "12345"
    assert b.geometry.bounds == (1.0, 2.0, 2.0, 3.0)


@respx.mock
async def test_tigerweb_zip_missing() -> None:
    respx.post(f"{TIGERWEB_BASE}/2/query").mock(return_value=fc())
    async with httpx.AsyncClient() as client:
        with pytest.raises(BoundaryNotFoundError):
            await tiger(client).resolve(AOISpec(zip="00000"))


@respx.mock
async def test_tigerweb_county_resolves_state_then_county() -> None:
    respx.post(f"{TIGERWEB_BASE}/80/query").mock(
        return_value=fc(
            feat({"GEOID": "77", "NAME": "Stateland", "STUSAB": "ST", "STATE": "77"}, None)
        )
    )
    county = respx.post(f"{TIGERWEB_BASE}/82/query").mock(
        return_value=fc(feat({"GEOID": "77001", "NAME": "Alpha County", "STATE": "77"}))
    )
    async with httpx.AsyncClient() as client:
        (b,) = await tiger(client).resolve(AOISpec(county=("Alpha County, ST",)))
    where = where_of(county.calls.last.request)
    assert "UPPER(NAME)='ALPHA COUNTY'" in where
    assert "STATE='77'" in where
    assert b.source.name == "Alpha County"


@respx.mock
async def test_tigerweb_county_falls_back_to_basename() -> None:
    respx.post(f"{TIGERWEB_BASE}/80/query").mock(
        return_value=fc(feat({"GEOID": "77", "NAME": "Stateland", "STATE": "77"}, None))
    )
    county = respx.post(f"{TIGERWEB_BASE}/82/query").mock(
        side_effect=[fc(), fc(feat({"GEOID": "77002", "NAME": "Beta Parish", "STATE": "77"}))]
    )
    async with httpx.AsyncClient() as client:
        (b,) = await tiger(client).resolve(AOISpec(county=("Beta Parish, ST",)))
    assert "BASENAME" in where_of(county.calls.last.request)
    assert b.source.source_id == "77002"


@respx.mock
async def test_tigerweb_place_prefers_incorporated_and_flags_ambiguity() -> None:
    respx.post(f"{TIGERWEB_BASE}/80/query").mock(
        return_value=fc(feat({"GEOID": "77", "NAME": "Stateland", "STATE": "77"}, None))
    )
    respx.post(f"{TIGERWEB_BASE}/28/query").mock(
        return_value=fc(feat({"GEOID": "7700001", "NAME": "Gamma city", "STATE": "77"}))
    )
    async with httpx.AsyncClient() as client:
        (b,) = await tiger(client).resolve(AOISpec(place="Gamma, ST"))
    assert b.source.name == "Gamma city"

    respx.post(f"{TIGERWEB_BASE}/28/query").mock(
        return_value=fc(
            feat({"GEOID": "1", "NAME": "Gamma city", "STATE": "77"}),
            feat({"GEOID": "2", "NAME": "Gamma town", "STATE": "77"}),
        )
    )
    async with httpx.AsyncClient() as client:
        with pytest.raises(BoundaryNotFoundError, match="ambiguous"):
            await tiger(client).resolve(AOISpec(place="Gamma, ST"))


@respx.mock
async def test_tigerweb_unknown_state() -> None:
    respx.post(f"{TIGERWEB_BASE}/80/query").mock(return_value=fc())
    async with httpx.AsyncClient() as client:
        with pytest.raises(BoundaryNotFoundError):
            await tiger(client).resolve(AOISpec(state="ZZ"))


# ---- WBD provider over mocked HTTP -------------------------------------------------


def wbd(client: httpx.AsyncClient, page_size: int = 2) -> WbdProvider:
    return WbdProvider(
        client, query_simplify_deg=0.0, boundary_simplify_deg=0.0, page_size=page_size
    )


def attrs(code: str) -> dict[str, Any]:
    return {"attributes": {"huc12": code, "name": f"N{code}", "areasqkm": 1.5, "states": "ST"}}


@respx.mock
async def test_wbd_covering_paginates_and_dedupes() -> None:
    route = respx.post(f"{WBD_BASE}/6/query").mock(
        side_effect=[
            httpx.Response(
                200,
                json={
                    "features": [attrs("000000000002"), attrs("000000000001")],
                    "exceededTransferLimit": True,
                },
            ),
            httpx.Response(200, json={"features": [attrs("000000000002"), attrs("000000000003")]}),
        ]
    )
    async with httpx.AsyncClient() as client:
        units = await wbd(client).covering(box(0, 0, 1, 1))
    assert [u.huc12 for u in units] == ["000000000001", "000000000002", "000000000003"]
    assert route.call_count == 2
    body = route.calls[0].request.content.decode()
    assert "esriGeometryPolygon" in body
    assert "resultOffset=0" in body
    assert "resultOffset=2" in route.calls.last.request.content.decode()


@respx.mock
async def test_wbd_units_under_prefix() -> None:
    route = respx.post(f"{WBD_BASE}/6/query").mock(
        return_value=httpx.Response(200, json={"features": [attrs("010100000001")]})
    )
    async with httpx.AsyncClient() as client:
        units = await wbd(client).units_under(["0101"])
    assert "LIKE+%270101%25%27" in route.calls.last.request.content.decode()
    assert units[0].area_sqkm == 1.5


@respx.mock
async def test_wbd_boundaries_picks_layer_by_digit_count() -> None:
    route = respx.post(f"{WBD_BASE}/4/query").mock(
        return_value=fc(feat({"huc8": "01010101", "name": "Basin"}))
    )
    async with httpx.AsyncClient() as client:
        (b,) = await wbd(client).boundaries(["01010101"])
    assert route.called
    assert b.source.name == "Basin"


@respx.mock
async def test_wbd_rejects_odd_length_codes() -> None:
    async with httpx.AsyncClient() as client:
        with pytest.raises(ValueError):
            await wbd(client).boundaries(["010"])


async def test_resolver_zip_uses_boundary_provider_and_labels_name() -> None:
    boundaries = FakeBoundaries()
    aoi = await AOIResolver(boundaries, FakeHucs()).resolve(AOISpec(zip="12345"))
    assert boundaries.calls == 1
    assert aoi.name == "ZIP 12345"
    assert aoi.sources[0].kind == "zip"
    assert aoi.bounds == (10.0, 20.0, 11.0, 21.0)
