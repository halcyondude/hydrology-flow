"""Region profiles: everything site-specific lives in ``profiles/<slug>/profile.yaml``."""

from __future__ import annotations

import re
from pathlib import Path, PurePosixPath
from typing import Literal, Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from hydrology_flow.aoi.models import LAT_MAX, LAT_MIN, LON_MAX, LON_MIN, AOISpec
from hydrology_flow.config import get_settings

PROFILE_FILENAME = "profile.yaml"
PRIVATE_PREFIX = PurePosixPath("data/private")
_SLUG = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")

SiteType = Literal["pond", "dam", "gauge", "wq_site", "parcel"]


class FocusSite(BaseModel):
    """A named thing the analysis centres on. Coordinates are optional: a site may be located
    later from its external ids or address (see ``docs/assumptions.md``)."""

    model_config = ConfigDict(extra="forbid")

    type: SiteType
    name: str
    lat: float | None = Field(default=None, ge=LAT_MIN, le=LAT_MAX)
    lon: float | None = Field(default=None, ge=LON_MIN, le=LON_MAX)
    address: str | None = None
    external_ids: dict[str, str] = Field(
        default_factory=dict,
        description="e.g. nid, comid, reachcode, usgs_site, massdep_au, ghcnd_station",
    )
    notes: str | None = None

    @model_validator(mode="after")
    def _lat_lon_pair(self) -> Self:
        if (self.lat is None) != (self.lon is None):
            raise ValueError(f"site {self.name!r}: lat and lon must be given together")
        return self

    @property
    def located(self) -> bool:
        return self.lat is not None


class ViewerCamera(BaseModel):
    """Opening camera for the 3D viewer. Targets are site names; numbers are optional overrides
    (when omitted the viewer frames the targets' extent)."""

    model_config = ConfigDict(extra="forbid")

    target_sites: list[str] = Field(min_length=1)
    range_m: float | None = Field(default=None, gt=0)
    heading_deg: float | None = None
    pitch_deg: float | None = None


class PrivateDataPointer(BaseModel):
    """A file or directory under the gitignored ``data/private/`` tree."""

    model_config = ConfigDict(extra="forbid")

    name: str
    path: str
    description: str = ""
    required: bool = False

    @field_validator("path")
    @classmethod
    def _under_private(cls, v: str) -> str:
        p = PurePosixPath(v)
        if p.is_absolute() or ".." in p.parts or not p.is_relative_to(PRIVATE_PREFIX):
            raise ValueError(f"private data path must be inside {PRIVATE_PREFIX}/: {v!r}")
        return v


class Profile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    slug: str
    display_name: str
    aoi: AOISpec
    focus_sites: list[FocusSite] = Field(default_factory=list)
    viewer: ViewerCamera | None = None
    private_data: list[PrivateDataPointer] = Field(default_factory=list)

    @field_validator("slug")
    @classmethod
    def _slug(cls, v: str) -> str:
        if not _SLUG.match(v):
            raise ValueError("slug must be lowercase kebab-case")
        return v

    @model_validator(mode="after")
    def _camera_targets_exist(self) -> Self:
        names = {s.name for s in self.focus_sites}
        if self.viewer:
            missing = [t for t in self.viewer.target_sites if t not in names]
            if missing:
                raise ValueError(f"viewer targets not in focus_sites: {missing}")
        return self

    def site(self, name: str) -> FocusSite:
        for s in self.focus_sites:
            if s.name == name:
                return s
        raise KeyError(name)


class ProfileNotFoundError(LookupError):
    pass


def profile_path(slug: str, root: Path | None = None) -> Path:
    if not _SLUG.match(slug):
        raise ProfileNotFoundError(f"invalid profile slug: {slug!r}")
    return (root or get_settings().profiles_dir) / slug / PROFILE_FILENAME


def load_profile(slug_or_path: str | Path, root: Path | None = None) -> Profile:
    """Load by slug (``worcester-ma``) or by explicit YAML path."""
    path = Path(slug_or_path)
    if path.suffix not in {".yaml", ".yml"}:
        path = profile_path(str(slug_or_path), root)
    if not path.is_file():
        raise ProfileNotFoundError(f"profile not found: {path}")
    return Profile.model_validate(yaml.safe_load(path.read_text()))


def list_profiles(root: Path | None = None) -> list[str]:
    root = root or get_settings().profiles_dir
    return sorted(p.parent.name for p in root.glob(f"*/{PROFILE_FILENAME}"))
