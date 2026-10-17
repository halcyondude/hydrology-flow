"""Schema and loader for ``data/sources.yaml``, the provenance register of public data."""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from hydrology_flow.config import get_settings


class DataSource(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(description="Stable kebab-case id referenced by pipeline stages")
    name: str
    publisher: str
    license: str = Field(description="License or public-domain statement")
    homepage: str
    access: list[str] = Field(description="Service or download URLs actually used")
    used_for: str
    citation: str | None = None
    notes: str | None = None


class SourceRegister(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int
    sources: list[DataSource]

    @model_validator(mode="after")
    def _unique_ids(self) -> SourceRegister:
        ids = [s.id for s in self.sources]
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        if dupes:
            raise ValueError(f"duplicate source ids: {dupes}")
        return self


def load_sources(path: Path | None = None) -> SourceRegister:
    path = path or get_settings().sources_path
    return SourceRegister.model_validate(yaml.safe_load(path.read_text()))
