"""Global settings (environment / .env driven) and named constants.

Nothing in this module names a place. Geography lives in ``profiles/<slug>/profile.yaml``.

Convention: any value that encodes a *modeling* judgement carries an ``# ASSUMPTION:``
comment with a sensitivity range (see CONTRIBUTING.md). Pure engineering knobs (timeouts,
retry counts) are named constants with a plain comment.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

ENV_PREFIX = "HFLOW_"

DEFAULT_CACHE_DIR = Path(".cache/hflow")
DEFAULT_DATA_DIR = Path("data")
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

# Page size for ArcGIS REST queries (service maxRecordCount is higher; stay below it).
DEFAULT_ARCGIS_PAGE_SIZE = 1000

TelemetryExporter = Literal["console", "otlp", "none"]


class Settings(BaseSettings):
    """Global settings. Override with ``HFLOW_<FIELD>`` environment variables or ``.env``."""

    model_config = SettingsConfigDict(
        env_prefix=ENV_PREFIX, env_file=".env", extra="ignore", frozen=True
    )

    cache_dir: Path = DEFAULT_CACHE_DIR
    data_dir: Path = DEFAULT_DATA_DIR
    log_level: str = DEFAULT_LOG_LEVEL

    # Telemetry: console by default; OTLP when an endpoint is configured.
    otlp_endpoint: str | None = None
    telemetry_exporter: TelemetryExporter | None = Field(
        default=None,
        description="console | otlp | none. Default: otlp if endpoint set, else console.",
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
    arcgis_page_size: int = Field(default=DEFAULT_ARCGIS_PAGE_SIZE, ge=1)

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
        return "otlp" if self.otlp_endpoint else "console"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
