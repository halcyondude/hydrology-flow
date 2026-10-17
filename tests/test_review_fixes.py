"""Regression tests for the PR #12 review findings (B1, W1-W5, N2-N4, N7)."""

from __future__ import annotations

import asyncio
import json
import random
import time
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from shapely import to_geojson
from shapely.geometry import MultiPolygon, Polygon, box

from hydrology_flow import cli
from hydrology_flow.aoi import AOIResolver, AOISpec, WbdProvider, arcgis
from hydrology_flow.aoi.arcgis import ArcGISError
from hydrology_flow.aoi.crs import recommend_projected_crs
from hydrology_flow.aoi.geometry import close_gaps, lon_extent, normalize_lon
from hydrology_flow.aoi.models import AOI, HucUnit, SourceBoundary
from hydrology_flow.aoi.providers import Boundary
from hydrology_flow.aoi.wbd import WBD_BASE
from hydrology_flow.config import Settings
from hydrology_flow.http import (
    CachingTransport,
    RateLimitTransport,
    RetryTransport,
    build_transport,
    cache_key,
)
from hydrology_flow.profiles import load_profile
from hydrology_flow.tiling import Manifest, plan_chunks, plan_id
from tests.conftest import REPO_ROOT, FakeBoundaries, FakeHucs
from tests.test_http import Sleeper, client_for

URL = "https://example.test/arcgis/query"
ERR_500 = {"error": {"code": 500, "message": "Error performing query operation"}}
ERR_400 = {"error": {"code": 400, "message": "Unable to complete operation."}}
OK = {"features": [{"attributes": {"huc12": "000000000001"}}]}


# ---- B1: ArcGIS errors served with HTTP 200 ------------------------------------------


@respx.mock
async def test_200_with_error_body_is_retried_and_not_cached(settings: Settings) -> None:
    route = respx.post(URL).mock(
        side_effect=[httpx.Response(200, json=ERR_500), httpx.Response(200, json=OK)]
    )
    sleeper = Sleeper()
    async with client_for(settings, sleeper) as client:
        feats = await arcgis.query(client, URL.removesuffix("/query"), {"f": "json"}, page_size=10)
    assert feats == OK["features"]
    assert route.call_count == 2
    assert len(sleeper.delays) == 1
    # Second client: the good answer is cached, the error never was.
    async with client_for(settings, Sleeper()) as client:
        await arcgis.query(client, URL.removesuffix("/query"), {"f": "json"}, page_size=10)
    assert route.call_count == 2


@respx.mock
async def test_permanent_error_body_is_not_retried_nor_cached(settings: Settings) -> None:
    route = respx.post(URL).mock(return_value=httpx.Response(200, json=ERR_400))
    sleeper = Sleeper()
    for _ in range(2):
        async with client_for(settings, sleeper) as client:
            with pytest.raises(ArcGISError):
                await arcgis.query(client, URL.removesuffix("/query"), {"f": "json"}, page_size=10)
    assert route.call_count == 2  # one per client: never served from cache
    assert sleeper.delays == []  # 4xx is the caller's fault: no retry


@respx.mock
async def test_exhausted_transient_error_is_not_cached(settings: Settings) -> None:
    limited = settings.model_copy(update={"http_max_retries": 1})
    route = respx.post(URL).mock(return_value=httpx.Response(200, json=ERR_500))
    for _ in range(2):
        async with client_for(limited, Sleeper()) as client:
            resp = await client.post(URL, data={"a": "b"})
            assert "error" in resp.json()
    assert route.call_count == 4


@respx.mock
async def test_previously_poisoned_cache_entry_is_ignored(settings: Settings) -> None:
    request = httpx.Request("POST", URL, data={"a": "b"})
    cache = CachingTransport(httpx.AsyncHTTPTransport(), settings.http_cache_dir)
    key = cache_key(request)
    meta, body = cache._paths(key)
    meta.parent.mkdir(parents=True)
    meta.write_text(json.dumps({"status": 200, "headers": []}))
    body.write_text(json.dumps(ERR_500))
    respx.post(URL).mock(return_value=httpx.Response(200, json=OK))
    async with client_for(settings, Sleeper()) as client:
        assert (await client.post(URL, data={"a": "b"})).json() == OK


def test_cli_reports_arcgis_error_cleanly(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    class Boom(FakeBoundaries):
        async def resolve(self, spec: AOISpec) -> list[Boundary]:
            raise ArcGISError("layer: {'code': 500}")

    monkeypatch.setattr(cli, "get_settings", lambda: settings)
    monkeypatch.setattr(cli, "default_resolver", lambda client, s: AOIResolver(Boom(), FakeHucs()))
    assert cli.main(["aoi", "--state", "ST"]) == 1
    err = capsys.readouterr().err
    assert "data service reported a failure" in err
    assert "Traceback" not in err


# ---- W5: paging --------------------------------------------------------------------


@respx.mock
@pytest.mark.parametrize("where", ["top", "properties"])
async def test_geojson_exceeded_transfer_limit_pages(where: str) -> None:
    def page(n: int, more: bool) -> httpx.Response:
        body: dict[str, Any] = {
            "type": "FeatureCollection",
            "features": [{"type": "Feature", "properties": {"i": n}, "geometry": None}],
        }
        if more:
            if where == "top":
                body["exceededTransferLimit"] = True
            else:
                body["properties"] = {"exceededTransferLimit": True}
        return httpx.Response(200, json=body)

    route = respx.post(URL).mock(side_effect=[page(0, True), page(1, True), page(2, False)])
    async with httpx.AsyncClient() as client:
        feats = await arcgis.query(
            client, URL.removesuffix("/query"), {"f": "geojson"}, page_size=1
        )
    assert [f["properties"]["i"] for f in feats] == [0, 1, 2]
    offsets = [route.calls[i].request.content.decode() for i in range(3)]
    assert "resultOffset=0" in offsets[0]
    assert "resultOffset=1" in offsets[1]
    assert "resultOffset=2" in offsets[2]


# ---- W1: slivers between independently generalized neighbours ------------------------


def holes(geom: Any) -> int:
    parts = list(geom.geoms) if hasattr(geom, "geoms") else [geom]
    return sum(len(p.interiors) for p in parts)


def test_close_gaps_removes_slivers_but_keeps_real_holes() -> None:
    sliver = box(0.9, 0.2, 0.9004, 0.8)  # 0.0004 deg wide: a generalization artifact
    enclave = box(1.3, 0.3, 1.7, 0.7)  # 0.4 deg wide: a real feature
    union = Polygon(
        box(0.0, 0.0, 2.0, 1.0).exterior.coords, [sliver.exterior.coords, enclave.exterior.coords]
    )
    assert holes(union) == 2
    closed = close_gaps(union, 0.001)
    assert holes(closed) == 1  # the enclave only
    assert close_gaps(union, 0.0) is union


async def test_resolver_closes_gaps_between_multiple_counties() -> None:
    class Adjacent(FakeBoundaries):
        async def resolve(self, spec: AOISpec) -> list[Boundary]:
            def b(name: str, geom: Polygon) -> Boundary:
                src = SourceBoundary(kind="county", name=name, source_id=name, source="f", url="")
                return Boundary(source=src, geometry=geom)

            return [b("A", box(0.0, 0.0, 1.0, 1.0)), b("B", box(1.0003, 0.0, 2.0, 1.0))]

    open_aoi = await AOIResolver(Adjacent(), FakeHucs(), gap_close_deg=0.0).resolve(
        AOISpec(county=("A, ST", "B, ST"))
    )
    assert isinstance(open_aoi.shape, MultiPolygon)  # tiles stay apart without closing
    closed = await AOIResolver(Adjacent(), FakeHucs(), gap_close_deg=0.001).resolve(
        AOISpec(county=("A, ST", "B, ST"))
    )
    assert closed.shape.geom_type == "Polygon"
    assert holes(closed.shape) == 0


# ---- W2: antimeridian ---------------------------------------------------------------


def aleutian_like() -> MultiPolygon:
    return MultiPolygon([box(172.0, 51.0, 180.0, 53.0), box(-180.0, 51.0, -170.0, 53.0)])


def test_lon_extent_wraps_across_antimeridian() -> None:
    west, east = lon_extent(aleutian_like())
    assert west == 172.0
    assert east == 190.0  # -170 + 360
    assert lon_extent(box(-170.0, 0.0, 170.0, 1.0)) == (-170.0, 170.0)  # genuinely wide, no wrap
    assert lon_extent(box(-72.0, 41.0, -71.0, 42.0)) == (-72.0, -71.0)


def test_crs_centre_is_antimeridian_aware() -> None:
    crs = recommend_projected_crs(aleutian_like())
    assert crs.startswith("+proj=laea")
    lon_0 = float(crs.split("+lon_0=")[1].split()[0])
    assert lon_0 == pytest.approx(normalize_lon(181.0))  # -179, not the naive 0 or +-90
    # Naive bounds-based centre would have been near 0.
    assert abs(lon_0) > 170


def test_alaska_like_extent() -> None:
    ak = MultiPolygon(
        [
            box(-168.0, 55.0, -130.0, 71.0),
            box(172.0, 51.0, 180.0, 53.0),
            box(-180.0, 51.0, -165.0, 53.0),
        ]
    )
    west, east = lon_extent(ak)
    assert (west, east) == (172.0, 230.0)
    lon_0 = float(recommend_projected_crs(ak).split("+lon_0=")[1].split()[0])
    assert lon_0 == pytest.approx(normalize_lon(201.0))


# ---- W3: overlap threshold ----------------------------------------------------------


def wbd(client: httpx.AsyncClient) -> WbdProvider:
    return WbdProvider(client, query_simplify_deg=0.0, boundary_simplify_deg=0.0, page_size=100)


def attrs(code: str) -> dict[str, Any]:
    return {"attributes": {"huc12": code, "name": code, "areasqkm": 1.0, "states": "ST"}}


def handler(request: httpx.Request) -> httpx.Response:
    body = request.content.decode()
    if "geometryType" in body and "esriSpatialRelContains" in body:
        return httpx.Response(200, json={"features": [attrs("000000000001")]})
    if "geometryType" in body:
        feats = [attrs("000000000001"), attrs("000000000002"), attrs("000000000003")]
        return httpx.Response(200, json={"features": feats})

    def feat(code: str, geom: Polygon) -> dict[str, Any]:
        return {
            "type": "Feature",
            "properties": {"huc12": code},
            "geometry": json.loads(to_geojson(geom)),
        }

    # AOI is box(0,0,10,10). 02 is 50% inside, 03 is a 0.1% sliver.
    return httpx.Response(
        200,
        json={
            "type": "FeatureCollection",
            "features": [
                feat("000000000002", box(9.0, 0.0, 11.0, 1.0)),
                feat("000000000003", box(9.999, 0.0, 20.0, 1.0)),
            ],
        },
    )


@respx.mock
async def test_covering_drops_units_below_overlap_threshold() -> None:
    respx.post(f"{WBD_BASE}/6/query").mock(side_effect=handler)
    aoi = box(0.0, 0.0, 10.0, 10.0)
    async with httpx.AsyncClient() as client:
        cover = await wbd(client).covering(aoi, 0.01)
        assert [u.huc12 for u in cover.units] == ["000000000001", "000000000002"]
        assert cover.dropped == 1
        strict = await wbd(client).covering(aoi, 0.6)
        assert [u.huc12 for u in strict.units] == ["000000000001"]
        assert strict.dropped == 2
        off = await wbd(client).covering(aoi, 0.0)
        assert len(off.units) == 3
        assert off.dropped == 0


async def test_resolver_reports_dropped_units() -> None:
    from hydrology_flow.aoi.providers import HucCover

    class Hucs(FakeHucs):
        async def covering(self, geometry: Any, min_overlap_fraction: float) -> HucCover:
            assert min_overlap_fraction == 0.01
            return HucCover([HucUnit(huc12="010100000101", name="x")], dropped=4)

    aoi = await AOIResolver(FakeBoundaries(), Hucs(), min_overlap_fraction=0.01).resolve(
        AOISpec(state="ST")
    )
    assert aoi.huc12_dropped == 4
    assert aoi.huc_min_overlap_fraction == 0.01
    assert aoi.to_feature()["properties"]["huc12_dropped_below_overlap"] == 4
    assert isinstance(aoi, AOI)


def test_default_overlap_threshold_is_documented_assumption() -> None:
    s = Settings(_env_file=None)
    assert s.huc_min_overlap_fraction == 0.01
    with pytest.raises(ValueError):
        Settings(_env_file=None, huc_min_overlap_fraction=1.5)
    text = (REPO_ROOT / "src/hydrology_flow/config.py").read_text()
    assert "sensitivity range 0 (keep" in text


# ---- W4 / N2: manifest scale and plan ids ---------------------------------------------


def big_aoi(n: int) -> AOI:
    units = [HucUnit(huc12=f"{i:012d}", name="x") for i in range(n)]
    return AOI(
        name="big",
        spec=AOISpec(state="ST"),
        geometry={"type": "Point", "coordinates": [0, 0]},
        projected_crs="x",
        bounds=(0, 0, 1, 1),
        area_sqkm=1,
        sources=[],
        huc12=units,
    )


def test_statewide_manifest_register_and_runnable_are_fast(tmp_path: Path) -> None:
    plan = plan_chunks(big_aoi(6377))
    with Manifest(tmp_path / "m.duckdb") as m:
        t0 = time.perf_counter()
        n = m.register(plan)
        t_register = time.perf_counter() - t0
        assert n == 6377 * len(plan.stages)
        t0 = time.perf_counter()
        assert len(m.runnable(plan, "acquire")) == 6377
        assert m.runnable(plan, "bake") == []
        t_runnable = time.perf_counter() - t0
        assert m.register(plan) == 0
    assert t_register < 2.0, t_register
    assert t_runnable < 2.0, t_runnable


def test_plan_id_includes_chunk_digits() -> None:
    aoi = big_aoi(150)
    assert plan_id(aoi, 12) != plan_id(aoi, 10)
    assert plan_chunks(aoi, chunk_digits=12).plan_id != plan_chunks(aoi, chunk_digits=10).plan_id
    with Manifest(":memory:") as m:
        m.register(plan_chunks(aoi, chunk_digits=12))
        m.register(plan_chunks(aoi, chunk_digits=10))
        fine, coarse = plan_chunks(aoi, chunk_digits=12), plan_chunks(aoi, chunk_digits=10)
        assert m.count(fine.plan_id) == 150 * len(fine.stages)
        assert m.count(coarse.plan_id) == 2 * len(coarse.stages)


# ---- N3: Retry-After pauses the host --------------------------------------------------


class Clock:
    def __init__(self) -> None:
        self.now = 100.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, d: float) -> None:
        self.slept.append(d)
        self.now += d


class Ok(httpx.AsyncBaseTransport):
    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, request=request)


async def test_paused_host_blocks_all_requests_to_it() -> None:
    clock = Clock()
    limiter = RateLimitTransport(
        Ok(), rate_per_second=1000.0, burst=1000, concurrency=4, clock=clock, sleep=clock.sleep
    )
    limiter.pause_host("a.test", 7.0)
    async with httpx.AsyncClient(transport=limiter) as client:
        await client.get("https://b.test/x")
        assert clock.slept == []  # other hosts unaffected
        await client.get("https://a.test/x")
    assert clock.slept == [pytest.approx(7.0)]


async def test_retry_after_on_429_pauses_host() -> None:
    seen: list[tuple[str, float]] = []

    class Throttled(httpx.AsyncBaseTransport):
        calls = 0

        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            Throttled.calls += 1
            if Throttled.calls == 1:
                return httpx.Response(429, headers={"Retry-After": "7"}, request=request)
            return httpx.Response(200, request=request)

    retry = RetryTransport(
        Throttled(),
        max_retries=2,
        backoff_base_s=0.1,
        backoff_max_s=1.0,
        retry_after_max_s=60.0,
        sleep=Sleeper(),
        rng=random.Random(0),
        on_retry_after=lambda host, d: seen.append((host, d)),
    )
    async with httpx.AsyncClient(transport=retry) as client:
        assert (await client.get("https://a.test/x")).status_code == 200
    assert len(seen) == 1
    assert seen[0][0] == "a.test"
    assert seen[0][1] == pytest.approx(7.0)


@respx.mock
async def test_build_transport_wires_pause(settings: Settings) -> None:
    respx.get("https://a.test/x").mock(
        side_effect=[httpx.Response(429, headers={"Retry-After": "7"}), httpx.Response(200)]
    )
    clock = Clock()
    transport = build_transport(
        settings.model_copy(update={"http_cache_enabled": False}),
        sleep=clock.sleep,
        clock=clock,
        rng=random.Random(0),
    )
    async with httpx.AsyncClient(transport=transport) as client:
        assert (await client.get("https://a.test/x")).status_code == 200
    assert max(clock.slept) >= 7.0


# ---- N4: unique temp names in cache writes ---------------------------------------------


async def test_concurrent_cache_writes_do_not_collide(tmp_path: Path) -> None:
    cache = CachingTransport(httpx.AsyncHTTPTransport(), tmp_path)
    req = httpx.Request("GET", "https://x.test/")
    resp = httpx.Response(200, content=b"payload", request=req)
    key = cache_key(req)
    await asyncio.gather(*(asyncio.to_thread(cache._write, key, resp) for _ in range(32)))
    assert cache._read(key) is not None
    assert not list(tmp_path.rglob("*.tmp"))


# ---- N7: paths do not depend on the working directory ----------------------------------


def test_default_paths_anchor_to_repo_root_not_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("HFLOW_HOME", raising=False)
    monkeypatch.chdir(tmp_path)
    s = Settings(_env_file=None)
    assert s.home == REPO_ROOT
    assert s.profiles_dir == REPO_ROOT / "profiles"
    assert s.sources_path == REPO_ROOT / "data" / "sources.yaml"
    assert s.cache_dir == REPO_ROOT / ".cache" / "hflow"
    assert load_profile("worcester-ma").slug == "worcester-ma"  # from an unrelated cwd


def test_hflow_home_override_and_relative_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HFLOW_HOME", str(tmp_path))
    monkeypatch.setenv("HFLOW_CACHE_DIR", "mycache")
    s = Settings(_env_file=None)
    assert s.home == tmp_path.resolve()
    assert s.cache_dir == tmp_path.resolve() / "mycache"
    assert s.data_dir == tmp_path.resolve() / "data"
    assert s.manifest_path == tmp_path.resolve() / "mycache" / "manifest.duckdb"
    absolute = tmp_path / "elsewhere"
    monkeypatch.setenv("HFLOW_CACHE_DIR", str(absolute))
    assert Settings(_env_file=None).cache_dir == absolute
