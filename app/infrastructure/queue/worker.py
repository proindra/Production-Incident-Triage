"""RQ task entry point for running the triage graph outside the API process."""

from __future__ import annotations

import logging
import socket
import time
from typing import Any

from app.core.config import DEFAULT_SETTINGS
from app.core.observability import (
    configure_observability,
    correlation_attributes,
    correlation_context,
)
from app.domain.schemas import TriageRequest
from app.infrastructure.diagnostics.mock import MockDiagnostic
from app.infrastructure.queue.queueing import (
    record_agent_metric,
    record_provider_metrics,
    record_worker_metrics,
)
from app.infrastructure.rag.config import RAGSettings
from app.infrastructure.llm.resilience import ProviderResilience
from app.infrastructure.storage.incidents import IncidentStore
from app.services.retrieval import RunbookRetriever
from app.services.triage import TriageEngine
from opentelemetry import propagate, trace
from opentelemetry.sdk.trace import TracerProvider
from redis.exceptions import RedisError
from langchain_groq import ChatGroq

logger = logging.getLogger(__name__)
configure_observability("incident-triage-worker", DEFAULT_SETTINGS.otel_endpoint)
tracer = trace.get_tracer("incident-triage-worker")
_rag_service = None
_rag_settings = RAGSettings.from_environment()
_provider_resilience = ProviderResilience(
    max_retries=_rag_settings.model_max_retries,
    initial_backoff_seconds=_rag_settings.retry_backoff_seconds,
    max_backoff_seconds=_rag_settings.retry_max_backoff_seconds,
    failure_threshold=_rag_settings.circuit_failure_threshold,
    reset_timeout_seconds=_rag_settings.circuit_reset_seconds,
)
if _rag_settings.enabled:
    _rag_settings.require_generation_settings()
if _rag_settings.agent_tool_selection_enabled:
    _rag_settings.require_tool_selection_settings()
    _tool_model = ChatGroq(
        api_key=_rag_settings.groq_api_key,
        model=_rag_settings.groq_model,
        base_url=_rag_settings.groq_base_url,
        temperature=0,
        timeout=_rag_settings.timeout_seconds,
        max_retries=0,
    )
else:
    _tool_model = None


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
        with correlation_context(correlation_id):
            with tracer.start_as_current_span(
                "incident.triage",
                context=parent_context,
                attributes=correlation_attributes(),
            ) as span:
                result = _process_triage(request_payload, correlation_id)
                span.set_attribute(
                    "rag.status", str(result.get("rag_status", "disabled"))
                )
                prompt_version = result.get("rag_prompt_version")
                if isinstance(prompt_version, str):
                    span.set_attribute(
                        "rag.prompt_version", prompt_version
                    )
                prompt_tokens = result.get("rag_prompt_tokens")
                if isinstance(prompt_tokens, int):
                    span.set_attribute(
                        "rag.prompt_tokens", prompt_tokens
                    )
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
        try:
            provider_snapshot = _provider_resilience.snapshot()
            record_provider_metrics(
                DEFAULT_SETTINGS.redis_url,
                socket.gethostname(),
                str(provider_snapshot["state"]),
                int(provider_snapshot["consecutive_failures"]),
                int(provider_snapshot["open_count"]),
            )
        except RedisError as error:
            logger.warning(
                "Provider circuit metrics could not be recorded (%s).",
                type(error).__name__,
                extra={"correlation_id": correlation_id},
            )


def _process_triage(
    request_payload: dict[str, Any], correlation_id: str
) -> dict[str, Any]:
    if DEFAULT_SETTINGS.triage_stub_delay_seconds > 0:
        time.sleep(DEFAULT_SETTINGS.triage_stub_delay_seconds)
    rag_service = _get_rag_service()
    engine = TriageEngine(
        diagnostic=MockDiagnostic(),
        retriever=RunbookRetriever(
            DEFAULT_SETTINGS.runbooks_path,
            postgres_docs_path=_rag_settings.postgres_docs_path,
        ),
        rag_service=rag_service,
        tool_model=_tool_model,
        model_name=_rag_settings.groq_model,
        input_cost_per_million=_rag_settings.input_cost_per_million,
        output_cost_per_million=_rag_settings.output_cost_per_million,
        provider_resilience=_provider_resilience,
        agent_metric_recorder=_record_agent_metric,
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

        _rag_service = RunbookRAG.from_settings(
            _rag_settings, provider_resilience=_provider_resilience
        )
    return _rag_service


def _record_agent_metric(agent: str, status: str, duration_ms: float) -> None:
    try:
        record_agent_metric(
            DEFAULT_SETTINGS.redis_url, agent, status, duration_ms
        )
    except RedisError as error:
        logger.warning(
            "Agent metrics could not be recorded (%s).",
            type(error).__name__,
        )
