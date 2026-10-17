"""Pluggable provider interfaces for AOI resolution."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from shapely.geometry.base import BaseGeometry

from hydrology_flow.aoi.models import AOISpec, HucUnit, SourceBoundary


@dataclass(frozen=True)
class Boundary:
    """A boundary polygon (EPSG:4326) and where it came from."""

    source: SourceBoundary
    geometry: BaseGeometry


@runtime_checkable
class BoundaryProvider(Protocol):
    """Turns a political-boundary selector (zip, place, county, state) into polygons."""

    async def resolve(self, spec: AOISpec) -> list[Boundary]: ...


@runtime_checkable
class HucProvider(Protocol):
    """Hydrologic-unit lookups. Implementations must return HUC12s sorted by code."""

    async def covering(self, geometry: BaseGeometry) -> list[HucUnit]:
        """All HUC12s intersecting ``geometry`` (EPSG:4326)."""
        ...

    async def boundaries(self, codes: Sequence[str]) -> list[Boundary]:
        """Polygons for hydrologic units of any level from 2 to 12 digits."""
        ...

    async def units_under(self, codes: Sequence[str]) -> list[HucUnit]:
        """All HUC12s whose code starts with any of ``codes`` (2-12 digits)."""
        ...
