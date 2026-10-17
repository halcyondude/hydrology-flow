"""OpenTelemetry wiring: traces, metrics and logs.

Console exporters write to stderr so stdout stays clean for CLI results.
Telemetry is off by default. Opt in with ``--telemetry console|otlp`` or
``HFLOW_TELEMETRY_EXPORTER``; setting ``HFLOW_OTLP_ENDPOINT`` implies OTLP over gRPC.
"""

from __future__ import annotations

import logging
import sys
from dataclasses import dataclass
from typing import IO

from opentelemetry import metrics, trace
from opentelemetry._logs import set_logger_provider
from opentelemetry.exporter.otlp.proto.grpc._log_exporter import OTLPLogExporter
from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
from opentelemetry.sdk._logs.export import (
    BatchLogRecordProcessor,
    ConsoleLogRecordExporter,
    LogRecordExporter,
)
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import (
    ConsoleMetricExporter,
    MetricExporter,
    PeriodicExportingMetricReader,
)
from opentelemetry.sdk.resources import SERVICE_NAME, Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    ConsoleSpanExporter,
    SpanExporter,
)

from hydrology_flow.config import Settings, get_settings

INSTRUMENTATION_NAME = "hydrology_flow"
# Metrics are exported on shutdown for the console exporter and periodically for OTLP.
METRIC_EXPORT_INTERVAL_MS = 30_000
LOGGER_NAMESPACE = "hydrology_flow"


@dataclass
class TelemetryHandle:
    tracer_provider: TracerProvider | None
    meter_provider: MeterProvider | None
    logger_provider: LoggerProvider | None
    log_handler: logging.Handler | None

    def shutdown(self) -> None:
        """Flush and stop every provider. Safe to call more than once."""
        if self.log_handler is not None:
            logging.getLogger(LOGGER_NAMESPACE).removeHandler(self.log_handler)
        for provider in (self.tracer_provider, self.meter_provider, self.logger_provider):
            if provider is not None:
                provider.shutdown()


_handle: TelemetryHandle | None = None


def _compact_span(span: object) -> str:
    return span.to_json(indent=None) + "\n"  # type: ignore[attr-defined, no-any-return]


def _compact_metrics(data: object) -> str:
    return data.to_json(indent=None) + "\n"  # type: ignore[attr-defined, no-any-return]


def _compact_log(record: object) -> str:
    return record.to_json(indent=None) + "\n"  # type: ignore[attr-defined, no-any-return]


def _exporters(
    settings: Settings, out: IO[str]
) -> tuple[SpanExporter, MetricExporter, LogRecordExporter]:
    if settings.resolved_exporter == "otlp":
        endpoint = settings.otlp_endpoint
        return (
            OTLPSpanExporter(endpoint=endpoint),
            OTLPMetricExporter(endpoint=endpoint),
            OTLPLogExporter(endpoint=endpoint),
        )
    return (
        ConsoleSpanExporter(out=out, formatter=_compact_span),
        ConsoleMetricExporter(out=out, formatter=_compact_metrics),
        ConsoleLogRecordExporter(out=out, formatter=_compact_log),
    )


def setup_telemetry(
    settings: Settings | None = None, *, out: IO[str] | None = None
) -> TelemetryHandle:
    """Install global OTel providers once. Later calls return the existing handle."""
    global _handle  # noqa: PLW0603
    if _handle is not None:
        return _handle
    settings = settings or get_settings()
    logging.getLogger(LOGGER_NAMESPACE).setLevel(settings.log_level.upper())
    if settings.resolved_exporter == "none":
        _handle = TelemetryHandle(None, None, None, None)
        return _handle

    resource = Resource.create({SERVICE_NAME: settings.service_name})
    span_exp, metric_exp, log_exp = _exporters(settings, out or sys.stderr)

    tracer_provider = TracerProvider(resource=resource)
    tracer_provider.add_span_processor(BatchSpanProcessor(span_exp))
    trace.set_tracer_provider(tracer_provider)

    reader = PeriodicExportingMetricReader(
        metric_exp, export_interval_millis=METRIC_EXPORT_INTERVAL_MS
    )
    meter_provider = MeterProvider(resource=resource, metric_readers=[reader])
    metrics.set_meter_provider(meter_provider)

    logger_provider = LoggerProvider(resource=resource)
    logger_provider.add_log_record_processor(BatchLogRecordProcessor(log_exp))
    set_logger_provider(logger_provider)
    handler = LoggingHandler(level=logging.NOTSET, logger_provider=logger_provider)
    logging.getLogger(LOGGER_NAMESPACE).addHandler(handler)

    _handle = TelemetryHandle(tracer_provider, meter_provider, logger_provider, handler)
    return _handle


def shutdown_telemetry() -> None:
    global _handle  # noqa: PLW0603
    if _handle is not None:
        _handle.shutdown()
        _handle = None


def get_tracer() -> trace.Tracer:
    return trace.get_tracer(INSTRUMENTATION_NAME)


def get_meter() -> metrics.Meter:
    return metrics.get_meter(INSTRUMENTATION_NAME)


def get_logger(name: str) -> logging.Logger:
    """Namespaced logger; records flow to the OTel handler once telemetry is set up."""
    return logging.getLogger(f"{LOGGER_NAMESPACE}.{name}")
