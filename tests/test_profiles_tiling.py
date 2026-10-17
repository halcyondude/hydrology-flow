from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from hydrology_flow.aoi import AOIResolver, AOISpec
from hydrology_flow.profiles import (
    PrivateDataPointer,
    Profile,
    ProfileNotFoundError,
    list_profiles,
    load_profile,
)
from hydrology_flow.sources import SourceRegister, load_sources
from hydrology_flow.tiling import STAGES, Manifest, aoi_id, plan_chunks
from tests.conftest import REPO_ROOT, FakeBoundaries, FakeHucs, make_unit

PROFILES = REPO_ROOT / "profiles"


# ---- profiles ----------------------------------------------------------------------


def test_reference_profile_loads() -> None:
    p = load_profile("worcester-ma", PROFILES)
    assert p.slug == "worcester-ma"
    assert p.aoi.county == ("Worcester County, MA",)
    dam = p.site("Cook's Pond Dam")
    assert dam.type == "dam"
    assert dam.external_ids["nid"] == "MA00123"
    assert dam.located
    assert not p.site("Smith Pond").located  # A1: unresolved, deliberately not invented
    assert p.site("Cook's Pond").external_ids["massdep_au"] == "MA51027"
    assert p.site("Donker Farm").address == "48-50 Tory Fort Ln"
    assert p.viewer is not None
    assert {s.type for s in p.focus_sites} <= {"pond", "dam", "gauge", "wq_site", "parcel"}


def test_all_private_pointers_are_under_data_private() -> None:
    for slug in list_profiles(PROFILES):
        for ptr in load_profile(slug, PROFILES).private_data:
            assert ptr.path.startswith("data/private/")


def test_profile_by_path_and_missing() -> None:
    assert load_profile(PROFILES / "worcester-ma" / "profile.yaml").slug == "worcester-ma"
    with pytest.raises(ProfileNotFoundError):
        load_profile("nope", PROFILES)
    with pytest.raises(ProfileNotFoundError):
        load_profile("../etc", PROFILES)


@pytest.mark.parametrize("path", ["/etc/passwd", "data/private/../../x", "data/public/x", "x"])
def test_private_pointer_rejects_escapes(path: str) -> None:
    with pytest.raises(ValidationError):
        PrivateDataPointer(name="n", path=path)


def _profile(**over: object) -> dict[str, object]:
    base: dict[str, object] = {
        "slug": "demo",
        "display_name": "Demo",
        "aoi": {"state": "ST"},
        "focus_sites": [{"type": "pond", "name": "P", "lat": 1.5, "lon": 2.5}],
    }
    return base | over


def test_profile_validation() -> None:
    assert Profile.model_validate(_profile()).site("P").located
    with pytest.raises(ValidationError):
        Profile.model_validate(_profile(slug="Bad Slug"))
    with pytest.raises(ValidationError):
        Profile.model_validate(_profile(focus_sites=[{"type": "lake", "name": "x"}]))
    with pytest.raises(ValidationError):
        Profile.model_validate(_profile(focus_sites=[{"type": "pond", "name": "x", "lat": 1.0}]))
    with pytest.raises(ValidationError):
        Profile.model_validate(_profile(viewer={"target_sites": ["missing"]}))
    with pytest.raises(ValidationError):
        Profile.model_validate(_profile(aoi={"zip": "12345", "state": "ST"}))


def test_profile_yaml_round_trip(tmp_path: Path) -> None:
    f = tmp_path / "profile.yaml"
    f.write_text(yaml.safe_dump(_profile()))
    assert load_profile(f).display_name == "Demo"


# ---- sources register --------------------------------------------------------------


def test_sources_yaml_valid() -> None:
    reg = load_sources(REPO_ROOT / "data" / "sources.yaml")
    assert {"census-tigerweb", "usgs-wbd"} <= {s.id for s in reg.sources}


def test_sources_reject_duplicates() -> None:
    src = {
        "id": "a",
        "name": "n",
        "publisher": "p",
        "license": "l",
        "homepage": "h",
        "access": [],
        "used_for": "u",
    }
    with pytest.raises(ValidationError):
        SourceRegister.model_validate({"schema_version": 1, "sources": [src, src]})


# ---- tiling ------------------------------------------------------------------------


async def _aoi(codes: list[str]):  # type: ignore[no-untyped-def]
    class Hucs(FakeHucs):
        async def covering(self, geometry):  # type: ignore[no-untyped-def]
            return [make_unit(c) for c in codes]

    return await AOIResolver(FakeBoundaries(), Hucs()).resolve(AOISpec(state="ST"))


async def test_plan_groups_by_chunk_level() -> None:
    aoi = await _aoi(["010100000101", "010100000102", "010100010101"])
    fine = plan_chunks(aoi)
    assert [c.code for c in fine.chunks] == ["010100000101", "010100000102", "010100010101"]
    coarse = plan_chunks(aoi, chunk_digits=10)
    assert [c.code for c in coarse.chunks] == ["0101000001", "0101000101"]
    assert coarse.chunks[0].huc12 == ("010100000101", "010100000102")
    with pytest.raises(ValueError):
        plan_chunks(aoi, chunk_digits=7)
    with pytest.raises(ValueError):
        plan_chunks(aoi, stages=["nope"])


async def test_aoi_id_depends_only_on_cover() -> None:
    a = await _aoi(["010100000101", "010100000102"])
    b = await _aoi(["010100000102", "010100000101"])
    c = await _aoi(["010100000101"])
    assert aoi_id(a) == aoi_id(b) != aoi_id(c)


async def test_manifest_register_is_idempotent_and_resumable(tmp_path: Path) -> None:
    aoi = await _aoi(["010100000101", "010100000102"])
    plan = plan_chunks(aoi)
    path = tmp_path / "m.duckdb"
    with Manifest(path) as m:
        assert m.register(plan) == 2 * len(STAGES)
        m.set_status(plan.aoi_id, "010100000101", "acquire", "done", artifact="a.parquet")
        m.set_status(plan.aoi_id, "010100000102", "acquire", "running")
    with Manifest(path) as m:  # reopen: progress survives, re-register adds nothing
        assert m.register(plan) == 0
        assert m.status(plan.aoi_id, "010100000101", "acquire") == "done"
        assert m.runnable(plan, "acquire") == []  # one done, one running
        assert m.runnable(plan, "condition") == ["010100000101"]  # needs acquire done
        assert m.reset_interrupted(plan.aoi_id) == 1
        assert m.runnable(plan, "acquire") == ["010100000102"]
        m.set_status(plan.aoi_id, "010100000102", "acquire", "failed", error="boom")
        assert m.runnable(plan, "acquire") == []
        assert m.runnable(plan, "acquire", retry_failed=True) == ["010100000102"]
        summary = m.summary(plan.aoi_id)
        assert summary["acquire"] == {"pending": 0, "running": 0, "done": 1, "failed": 1}
        assert summary["bake"]["pending"] == 2


async def test_manifest_unknown_row_errors() -> None:
    with Manifest(":memory:") as m:
        with pytest.raises(KeyError):
            m.set_status("x", "y", "acquire", "done")
        with pytest.raises(KeyError):
            m.status("x", "y", "acquire")
