"""Global settings (environment / .env driven) and named constants.

Nothing in this module names a place. Geography lives in ``profiles/<slug>/profile.yaml``.

Convention: any value that encodes a *modeling* judgement carries an ``# ASSUMPTION:``
comment with a sensitivity range (see CONTRIBUTING.md). Pure engineering knobs (timeouts,
retry counts) are named constants with a plain comment.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ENV_PREFIX = "HFLOW_"

HOME_ENV = "HFLOW_HOME"
CACHE_SUBDIR = ".cache/hflow"
DATA_SUBDIR = "data"
PROFILES_SUBDIR = "profiles"
SOURCES_FILENAME = "sources.yaml"
PRIVATE_DATA_SUBDIR = "private"

SERVICE_NAME = "hydrology-flow"
DEFAULT_LOG_LEVEL = "INFO"

# --- HTTP engineering knobs (not modeling assumptions) ---
DEFAULT_HTTP_CONCURRENCY = 8
DEFAULT_HTTP_RATE_PER_SECOND = 5.0
DEFAULT_HTTP_BURST = 10
DEFAULT_HTTP_TIMEOUT_S = 60.0
DEFAULT_HTTP_MAX_RETRIES = 4
DEFAULT_HTTP_BACKOFF_BASE_S = 0.5
DEFAULT_HTTP_BACKOFF_MAX_S = 30.0
DEFAULT_HTTP_RETRY_AFTER_MAX_S = 120.0

# ASSUMPTION: AOI boundary generalization tolerance, in degrees of EPSG:4326, applied by the
# boundary service (maxAllowableOffset) so statewide polygons stay transferable.
# 5e-4 deg is roughly 50 m. Sensitivity range: 0 (full resolution) to 5e-3 (~500 m). It only
# affects the stored AOI outline and the HUC12 cover at the margin; watershed analysis always
# expands to whole HUC12s, so the effect on results is bounded by one edge HUC12 ring.
DEFAULT_AOI_SIMPLIFY_DEG = 5e-4

# ASSUMPTION: tolerance (degrees) used to simplify the polygon sent to the WBD intersect query.
# Coarser than the stored outline to keep POST bodies small. 2e-3 deg is roughly 200 m.
# Sensitivity range: 5e-4 to 5e-3. Marginal HUC12s with a sliver of overlap may be added or
# dropped; the covering set errs on inclusion because HUC12s are never clipped.
DEFAULT_HUC_QUERY_SIMPLIFY_DEG = 2e-3

# ASSUMPTION: minimum fraction of a HUC12's area that must lie inside the AOI for it to join the
# covering set. Drops HUC12s that only graze the AOI (for example ones across a drainage divide
# that the political boundary clips by a sliver). Default 0.01 (1%); sensitivity range 0 (keep
# any intersecting HUC12) to 0.05 (5%). Overlap is measured against the best-available AOI
# geometry (generalized by DEFAULT_AOI_SIMPLIFY_DEG). Units dropped are counted in the AOI output.
DEFAULT_HUC_MIN_OVERLAP_FRACTION = 0.01
HUC_MIN_OVERLAP_MAX = 1.0

# ASSUMPTION: multi-boundary unions are closed with buffer(+e).buffer(-e) so slivers between
# neighbours that were generalized independently disappear. e = this factor x aoi_simplify_deg.
# Default 2 (a gap can be up to two offsets wide); sensitivity range 1 to 4. Gaps wider than e
# (a real enclave or lake) are kept.
GAP_CLOSE_FACTOR = 2.0

# Page size for ArcGIS REST queries (service maxRecordCount is higher; stay below it).
DEFAULT_ARCGIS_PAGE_SIZE = 1000

TelemetryExporter = Literal["console", "otlp", "none"]
_ROOT_MARKERS = ("pyproject.toml", PROFILES_SUBDIR)


def default_home() -> Path:
    """``HFLOW_HOME`` if set, else the repo root containing this checkout, else the cwd."""
    if env := os.environ.get(HOME_ENV):
        return Path(env).resolve()
    candidate = Path(__file__).resolve().parents[2]
    if all((candidate / m).exists() for m in _ROOT_MARKERS):
        return candidate
    return Path.cwd()


class Settings(BaseSettings):
    """Global settings. Override with ``HFLOW_<FIELD>`` environment variables or ``.env``."""

    model_config = SettingsConfigDict(
        env_prefix=ENV_PREFIX, env_file=".env", extra="ignore", frozen=True
    )

    home: Path = Field(default_factory=default_home, description="Repo root")
    cache_dir: Path = Path()
    data_dir: Path = Path()
    profiles_dir: Path = Path()
    log_level: str = DEFAULT_LOG_LEVEL

    # Telemetry: off by default; opt in with console or otlp (otlp is implied by an endpoint).
    otlp_endpoint: str | None = None
    telemetry_exporter: TelemetryExporter | None = Field(
        default=None,
        description="console | otlp | none. Default: otlp if an endpoint is set, else none.",
    )
    service_name: str = SERVICE_NAME

    # HTTP
    http_concurrency: int = Field(default=DEFAULT_HTTP_CONCURRENCY, ge=1)
    http_rate_per_second: float = Field(default=DEFAULT_HTTP_RATE_PER_SECOND, gt=0)
    http_burst: int = Field(default=DEFAULT_HTTP_BURST, ge=1)
    http_timeout_s: float = Field(default=DEFAULT_HTTP_TIMEOUT_S, gt=0)
    http_max_retries: int = Field(default=DEFAULT_HTTP_MAX_RETRIES, ge=0)
    http_backoff_base_s: float = Field(default=DEFAULT_HTTP_BACKOFF_BASE_S, ge=0)
    http_backoff_max_s: float = Field(default=DEFAULT_HTTP_BACKOFF_MAX_S, ge=0)
    http_retry_after_max_s: float = Field(default=DEFAULT_HTTP_RETRY_AFTER_MAX_S, ge=0)
    http_cache_enabled: bool = True

    # AOI (see ASSUMPTION notes above)
    aoi_simplify_deg: float = Field(default=DEFAULT_AOI_SIMPLIFY_DEG, ge=0)
    huc_query_simplify_deg: float = Field(default=DEFAULT_HUC_QUERY_SIMPLIFY_DEG, ge=0)
    huc_min_overlap_fraction: float = Field(
        default=DEFAULT_HUC_MIN_OVERLAP_FRACTION, ge=0, le=HUC_MIN_OVERLAP_MAX
    )
    arcgis_page_size: int = Field(default=DEFAULT_ARCGIS_PAGE_SIZE, ge=1)

    @model_validator(mode="before")
    @classmethod
    def _anchor_paths(cls, values: Any) -> Any:
        """Default and relative paths resolve against ``home``, never the working directory."""
        if not isinstance(values, dict):
            return values
        home = Path(values.get("home") or default_home())
        out = dict(values)
        out["home"] = home
        for field, default in (
            ("cache_dir", CACHE_SUBDIR),
            ("data_dir", DATA_SUBDIR),
            ("profiles_dir", PROFILES_SUBDIR),
        ):
            path = Path(out.get(field) or default)
            out[field] = path if path.is_absolute() else home / path
        return out

    @property
    def sources_path(self) -> Path:
        return self.data_dir / SOURCES_FILENAME

    @property
    def http_cache_dir(self) -> Path:
        return self.cache_dir / "http"

    @property
    def aoi_cache_dir(self) -> Path:
        return self.cache_dir / "aoi"

    @property
    def manifest_path(self) -> Path:
        return self.cache_dir / "manifest.duckdb"

    @property
    def private_data_dir(self) -> Path:
        return self.data_dir / PRIVATE_DATA_SUBDIR

    @property
    def resolved_exporter(self) -> TelemetryExporter:
        if self.telemetry_exporter is not None:
            return self.telemetry_exporter
        return "otlp" if self.otlp_endpoint else "none"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
