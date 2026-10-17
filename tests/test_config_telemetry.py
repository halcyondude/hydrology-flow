from __future__ import annotations

import io
import json
import time
from pathlib import Path

import pytest

from hydrology_flow import telemetry
from hydrology_flow.config import Settings


def make(**kw: object) -> Settings:
    return Settings(_env_file=None, **kw)  # type: ignore[arg-type]


def test_defaults_are_sane() -> None:
    s = make()
    assert s.http_concurrency >= 1
    assert s.resolved_exporter == "none"  # telemetry is opt-in
    assert s.http_cache_dir == s.cache_dir / "http"
    assert s.private_data_dir == s.data_dir / "private"


def test_env_overrides(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HFLOW_CACHE_DIR", str(tmp_path))
    monkeypatch.setenv("HFLOW_HTTP_CONCURRENCY", "3")
    monkeypatch.setenv("HFLOW_OTLP_ENDPOINT", "http://collector.invalid:4318")
    s = make()
    assert s.cache_dir == tmp_path
    assert s.http_concurrency == 3
    assert s.resolved_exporter == "otlp"


def test_explicit_exporter_beats_endpoint() -> None:
    assert (
        make(otlp_endpoint="http://x.invalid", telemetry_exporter="console").resolved_exporter
        == "console"
    )


def test_invalid_values_rejected() -> None:
    with pytest.raises(ValueError):
        make(http_concurrency=0)
    with pytest.raises(ValueError):
        make(http_rate_per_second=0)


def test_console_exporter_emits_span_and_log_to_stream() -> None:
    telemetry.shutdown_telemetry()
    out = io.StringIO()
    handle = telemetry.setup_telemetry(make(telemetry_exporter="console"), out=out)
    assert telemetry.setup_telemetry(make(telemetry_exporter="none")) is handle  # idempotent
    with telemetry.get_tracer().start_as_current_span("unit-test-span"):
        telemetry.get_logger("t").warning("hello telemetry")
    telemetry.get_meter().create_counter("unit.test").add(1)
    telemetry.shutdown_telemetry()
    time.sleep(0)
    lines = [json.loads(line) for line in out.getvalue().splitlines() if line.strip()]
    assert any(rec.get("name") == "unit-test-span" for rec in lines)
    assert any("hello telemetry" in json.dumps(rec) for rec in lines)


def test_none_exporter_is_a_noop() -> None:
    telemetry.shutdown_telemetry()
    handle = telemetry.setup_telemetry(make(telemetry_exporter="none"))
    assert handle.tracer_provider is None
    handle.shutdown()
    telemetry.shutdown_telemetry()
