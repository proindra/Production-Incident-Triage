"""RQ task entry point for running the triage graph outside the API process."""

from __future__ import annotations

import logging
import time
from typing import Any

from app.core.config import DEFAULT_SETTINGS
from app.core.observability import configure_observability
from app.domain.schemas import TriageRequest
from app.infrastructure.diagnostics.mock import MockDiagnostic
from app.infrastructure.diagnostics.postgres_readonly import (
    PostgresReadOnlyDiagnostic,
)
from app.infrastructure.queue.queueing import record_worker_metrics
from app.infrastructure.rag.config import RAGSettings
from app.infrastructure.storage.incidents import IncidentStore
from app.services.retrieval import RunbookRetriever
from app.services.triage import TriageEngine
from opentelemetry import propagate, trace
from opentelemetry.sdk.trace import TracerProvider
from redis.exceptions import RedisError

logger = logging.getLogger(__name__)
configure_observability("incident-triage-worker", DEFAULT_SETTINGS.otel_endpoint)
tracer = trace.get_tracer("incident-triage-worker")
_rag_service = None
_rag_settings = RAGSettings.from_environment()
if _rag_settings.enabled:
    _rag_settings.require_generation_settings()


def process_triage(
    request_payload: dict[str, Any],
    correlation_id: str,
    trace_context: dict[str, str] | None = None,
) -> dict[str, Any]:
    started = time.perf_counter()
    result: dict[str, Any] | None = None
    status = "failure"
    parent_context = propagate.extract(trace_context or {})
    try:
        with tracer.start_as_current_span(
            "incident.triage",
            context=parent_context,
            attributes={"correlation_id": correlation_id},
        ) as span:
            result = _process_triage(request_payload, correlation_id)
            span.set_attribute("rag.status", str(result.get("rag_status", "disabled")))
            prompt_version = result.get("rag_prompt_version")
            if isinstance(prompt_version, str):
                span.set_attribute("rag.prompt_version", prompt_version)
            prompt_tokens = result.get("rag_prompt_tokens")
            if isinstance(prompt_tokens, int):
                span.set_attribute("rag.prompt_tokens", prompt_tokens)
            completion_tokens = result.get("rag_completion_tokens")
            if isinstance(completion_tokens, int):
                span.set_attribute(
                    "rag.completion_tokens", completion_tokens
                )
        status = "success"
        return result
    finally:
        provider = trace.get_tracer_provider()
        if isinstance(provider, TracerProvider) and not provider.force_flush(
            timeout_millis=5_000
        ):
            logger.warning(
                "Worker trace export did not flush before job exit.",
                extra={"correlation_id": correlation_id},
            )
        try:
            record_worker_metrics(
                DEFAULT_SETTINGS.redis_url,
                status,
                time.perf_counter() - started,
                result,
            )
        except RedisError as error:
            logger.warning(
                "Worker metrics could not be recorded (%s).",
                type(error).__name__,
                extra={"correlation_id": correlation_id},
            )


def _process_triage(
    request_payload: dict[str, Any], correlation_id: str
) -> dict[str, Any]:
    if DEFAULT_SETTINGS.triage_stub_delay_seconds > 0:
        time.sleep(DEFAULT_SETTINGS.triage_stub_delay_seconds)
    diagnostic = (
        PostgresReadOnlyDiagnostic(DEFAULT_SETTINGS.database_url)
        if DEFAULT_SETTINGS.database_url
        else MockDiagnostic()
    )
    rag_service = _get_rag_service()
    engine = TriageEngine(
        diagnostic=diagnostic,
        retriever=RunbookRetriever(DEFAULT_SETTINGS.runbooks_path),
        rag_service=rag_service,
    )
    request = TriageRequest.model_validate(request_payload)
    result = engine.triage(request)
    result.incident_id = IncidentStore(DEFAULT_SETTINGS.incident_db_path).record(result)
    logger.info(
        "Triage run completed",
        extra={
            "correlation_id": correlation_id,
            "incident_id": result.incident_id,
            "category": result.category,
            "severity": result.severity,
            "rag_status": result.rag_status,
            "rag_prompt_version": result.rag_prompt_version,
            "rag_prompt_tokens": result.rag_prompt_tokens,
            "rag_completion_tokens": result.rag_completion_tokens,
        },
    )
    return result.model_dump(mode="json")


def _get_rag_service():
    if not _rag_settings.enabled:
        return None
    global _rag_service
    if _rag_service is None:
        from app.infrastructure.rag.graph_builder import RunbookRAG

        _rag_service = RunbookRAG.from_settings(_rag_settings)
    return _rag_service
