from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import pytest
from shapely.geometry import box
from shapely.geometry.base import BaseGeometry

from hydrology_flow.aoi.models import AOISpec, HucUnit, SourceBoundary
from hydrology_flow.aoi.providers import Boundary
from hydrology_flow.config import Settings

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        cache_dir=tmp_path / "cache",
        telemetry_exporter="none",
        http_rate_per_second=1000.0,
        http_burst=1000,
    )


def make_unit(code: str, name: str = "Unit") -> HucUnit:
    return HucUnit(huc12=code, name=name, area_sqkm=10.0, states="XX")


class FakeBoundaries:
    """Boundary provider returning a fixed 1x1 degree square."""

    def __init__(self) -> None:
        self.calls = 0

    async def resolve(self, spec: AOISpec) -> list[Boundary]:
        self.calls += 1
        src = SourceBoundary(
            kind=spec.kind, name="Fake Place", source_id="99999", source="fake", url="fake://"
        )
        return [Boundary(source=src, geometry=box(10.0, 20.0, 11.0, 21.0))]


class FakeHucs:
    def __init__(self) -> None:
        self.calls = 0

    async def covering(self, geometry: BaseGeometry) -> list[HucUnit]:
        self.calls += 1
        return [make_unit("010100000101"), make_unit("010100000102")]

    async def boundaries(self, codes: Sequence[str]) -> list[Boundary]:
        return [
            Boundary(
                source=SourceBoundary(
                    kind="huc", name=f"HUC {c}", source_id=c, source="fake", url="fake://"
                ),
                geometry=box(0.0, 0.0, 1.0, 1.0),
            )
            for c in codes
        ]

    async def units_under(self, codes: Sequence[str]) -> list[HucUnit]:
        return [make_unit(f"{c:0<12}"[:12]) for c in codes]
