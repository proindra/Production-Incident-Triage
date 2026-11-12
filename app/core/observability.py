"""Structured logs and optional OpenTelemetry export."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor


class JsonLogFormatter(logging.Formatter):
    _FIELDS = (
        "correlation_id",
        "incident_id",
        "run_id",
        "category",
        "severity",
        "method",
        "route",
        "status_code",
        "duration_ms",
        "rag_status",
        "rag_prompt_version",
        "rag_prompt_tokens",
        "rag_completion_tokens",
    )

    def format(self, record: logging.LogRecord) -> str:
        event: dict[str, object] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for field in self._FIELDS:
            if hasattr(record, field):
                event[field] = getattr(record, field)
        return json.dumps(event, sort_keys=True, default=str)


def configure_observability(service_name: str, otel_endpoint: str | None) -> None:
    root_logger = logging.getLogger()
    if not any(isinstance(handler.formatter, JsonLogFormatter) for handler in root_logger.handlers):
        handler = logging.StreamHandler()
        handler.setFormatter(JsonLogFormatter())
        root_logger.addHandler(handler)
        if root_logger.level > logging.INFO:
            root_logger.setLevel(logging.INFO)

    if not otel_endpoint:
        return
    if isinstance(trace.get_tracer_provider(), TracerProvider):
        return
    provider = TracerProvider(resource=Resource.create({"service.name": service_name}))
    provider.add_span_processor(
        BatchSpanProcessor(OTLPSpanExporter(endpoint=otel_endpoint, insecure=True))
    )
    trace.set_tracer_provider(provider)
