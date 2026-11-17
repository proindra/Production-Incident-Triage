"""FastAPI routes and the minimal browser-based triage interface."""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest
from opentelemetry import propagate, trace
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from uuid import UUID

from app.core.config import DEFAULT_SETTINGS
from app.core.observability import configure_observability, correlation_context
from app.domain.schemas import (
    FeedbackAccepted,
    FeedbackRequest,
    FeedbackSummary,
    HealthResponse,
    RunAccepted,
    RunStatus,
    TriageRequest,
)
from app.infrastructure.queue.queueing import QueueError, RedisTaskQueue
from app.infrastructure.storage.incidents import (
    IncidentNotFoundError,
    IncidentStore,
    StorageError,
)

store = IncidentStore(DEFAULT_SETTINGS.incident_db_path)
task_queue = RedisTaskQueue(DEFAULT_SETTINGS.redis_url)
configure_observability("incident-triage-api", DEFAULT_SETTINGS.otel_endpoint)
app = FastAPI(title="Incident Runbook Triage Assistant", version="0.1.0")
FastAPIInstrumentor.instrument_app(app)
REQUESTS = Counter(
    "incident_triage_http_requests_total",
    "HTTP requests served by the triage API.",
    ("method", "route", "status"),
)
REQUEST_LATENCY = Histogram(
    "incident_triage_http_request_duration_seconds",
    "HTTP request duration in seconds.",
    ("method", "route"),
)
logger = logging.getLogger(__name__)


@app.middleware("http")
async def observe_request(request: Request, call_next) -> Response:
    correlation_id = request.headers.get("X-Correlation-ID", "")
    if not correlation_id or len(correlation_id) > 128 or not correlation_id.isascii():
        correlation_id = str(uuid.uuid4())
    request.state.correlation_id = correlation_id
    trace.get_current_span().set_attribute("correlation_id", correlation_id)
    started = time.perf_counter()
    with correlation_context(correlation_id):
        response = await call_next(request)
    elapsed = time.perf_counter() - started
    route = getattr(request.scope.get("route"), "path", "unmatched")
    REQUESTS.labels(request.method, route, str(response.status_code)).inc()
    REQUEST_LATENCY.labels(request.method, route).observe(elapsed)
    response.headers["X-Correlation-ID"] = correlation_id
    logger.info(
        "HTTP request completed",
        extra={
            "correlation_id": correlation_id,
            "method": request.method,
            "route": route,
            "status_code": response.status_code,
            "duration_ms": round(elapsed * 1000, 3),
        },
    )
    return response


@app.exception_handler(RequestValidationError)
async def quarantine_invalid_request(
    request: Request, error: RequestValidationError
) -> JSONResponse:
    raw_payload = (await request.body()).decode("utf-8", errors="replace")
    try:
        payload = json.loads(raw_payload)
        payload_summary = json.dumps(payload, default=str)
    except json.JSONDecodeError:
        payload_summary = raw_payload
    field_errors = [
        {
            "field": ".".join(str(part) for part in item.get("loc", ())),
            "type": str(item.get("type", "validation_error")),
            "message": str(item.get("msg", "Invalid request.")),
        }
        for item in error.errors()
    ]
    try:
        quarantine_id = store.quarantine(payload_summary, field_errors)
    except StorageError as storage_error:
        raise HTTPException(status_code=503, detail=str(storage_error)) from None
    return JSONResponse(
        status_code=422,
        content={
            "detail": field_errors,
            "quarantine_id": quarantine_id,
            "correlation_id": request.state.correlation_id,
        },
    )


@app.get("/health", response_model=HealthResponse, tags=["health"])
def health() -> HealthResponse:
    return HealthResponse(status="ok")


@app.get("/ready", response_model=HealthResponse, tags=["health"])
def ready() -> HealthResponse:
    try:
        if not task_queue.ping():
            raise HTTPException(status_code=503, detail="The Redis task queue is unavailable.")
    except QueueError as error:
        raise HTTPException(status_code=503, detail=str(error)) from None
    return HealthResponse(status="ok")


@app.post(
    "/incidents/triage",
    response_model=RunAccepted,
    status_code=202,
    tags=["triage"],
)
def triage(request: TriageRequest, http_request: Request) -> RunAccepted:
    client_id = http_request.client.host if http_request.client else "unknown"
    try:
        allowed = task_queue.allow_request(
            client_id,
            limit=DEFAULT_SETTINGS.rate_limit_per_minute,
        )
    except QueueError as error:
        raise HTTPException(status_code=503, detail=str(error)) from None
    if not allowed:
        raise HTTPException(
            status_code=429,
            detail="Rate limit exceeded; try again after the current minute.",
        )
    try:
        trace_context: dict[str, str] = {}
        propagate.inject(trace_context)
        run_id = task_queue.enqueue(
            request.model_dump(mode="json"),
            http_request.state.correlation_id,
            trace_context,
        )
    except QueueError as error:
        raise HTTPException(status_code=503, detail=str(error)) from None
    return RunAccepted(
        run_id=run_id,
        status="queued",
        status_url=f"/incidents/runs/{run_id}",
    )


@app.get("/incidents/runs/{run_id}", response_model=RunStatus, tags=["triage"])
def run_status(run_id: str) -> RunStatus:
    try:
        result = task_queue.status(run_id)
    except QueueError as error:
        raise HTTPException(status_code=503, detail=str(error)) from None
    if result is None:
        raise HTTPException(status_code=404, detail="Run not found.")
    return RunStatus.model_validate(result)


@app.get("/metrics", include_in_schema=False)
def metrics() -> Response:
    try:
        worker_metrics = task_queue.worker_metrics()
    except QueueError as error:
        raise HTTPException(status_code=503, detail=str(error)) from None
    output = (
        generate_latest().decode("utf-8")
        + _worker_metrics_text(worker_metrics)
        + _index_metrics_text()
    )
    return Response(output, media_type=CONTENT_TYPE_LATEST)


def _worker_metrics_text(values: dict[str, str]) -> str:
    lines = [
        "# HELP incident_triage_worker_runs_total Completed and failed worker runs.",
        "# TYPE incident_triage_worker_runs_total counter",
        f'incident_triage_worker_runs_total{{status="success"}} {values.get("runs:success", "0")}',
        f'incident_triage_worker_runs_total{{status="failure"}} {values.get("runs:failure", "0")}',
        "# HELP incident_triage_worker_run_duration_seconds End-to-end worker execution duration.",
        "# TYPE incident_triage_worker_run_duration_seconds histogram",
    ]
    for bucket in (0.5, 1, 2.5, 5, 10, 30, 60, 120):
        lines.append(
            "incident_triage_worker_run_duration_seconds_bucket"
            f'{{le="{bucket}"}} {values.get(f"duration_bucket:{bucket}", "0")}'
        )
    lines.extend(
        [
            "incident_triage_worker_run_duration_seconds_bucket"
            f'{{le="+Inf"}} {values.get("duration_bucket:+Inf", "0")}',
            "incident_triage_worker_run_duration_seconds_sum "
            + values.get("duration_sum", "0"),
            "incident_triage_worker_run_duration_seconds_count "
            + values.get("duration_count", "0"),
            "# HELP incident_triage_agent_steps_total LangGraph agent steps across runs.",
            "# TYPE incident_triage_agent_steps_total counter",
            "incident_triage_agent_steps_total " + values.get("agent_steps", "0"),
            "# HELP incident_triage_tool_calls_total Tool calls across runs.",
            "# TYPE incident_triage_tool_calls_total counter",
            "incident_triage_tool_calls_total " + values.get("tool_calls", "0"),
        ]
    )
    lines.extend(
        [
            "# HELP incident_triage_retrieval_retries_total Alternate retrieval attempts.",
            "# TYPE incident_triage_retrieval_retries_total counter",
            "incident_triage_retrieval_retries_total "
            + values.get("retrieval_retries", "0"),
            "# HELP incident_triage_model_prompt_tokens_total Provider prompt tokens.",
            "# TYPE incident_triage_model_prompt_tokens_total counter",
            "incident_triage_model_prompt_tokens_total "
            + values.get("prompt_tokens", "0"),
            "# HELP incident_triage_model_completion_tokens_total Provider completion tokens.",
            "# TYPE incident_triage_model_completion_tokens_total counter",
            "incident_triage_model_completion_tokens_total "
            + values.get("completion_tokens", "0"),
            "# HELP incident_triage_model_cost_usd_total Estimated provider cost from configured rates.",
            "# TYPE incident_triage_model_cost_usd_total counter",
            "incident_triage_model_cost_usd_total "
            + values.get("model_cost_usd", "0"),
            "# HELP incident_triage_agent_calls_total Completed agent steps.",
            "# TYPE incident_triage_agent_calls_total counter",
            "# HELP incident_triage_agent_success_total Successful agent steps.",
            "# TYPE incident_triage_agent_success_total counter",
            "# HELP incident_triage_agent_failure_total Failed agent steps.",
            "# TYPE incident_triage_agent_failure_total counter",
            "# HELP incident_triage_agent_duration_seconds Agent step latency distribution.",
            "# TYPE incident_triage_agent_duration_seconds histogram",
        ]
    )
    for agent in ("triage", "rag_generation", "verification", "alternate_search"):
        prefix = f"agent:{agent}:"
        calls = values.get(prefix + "calls", "0")
        successes = values.get(prefix + "success", "0")
        failures = values.get(prefix + "failure", "0")
        lines.extend(
            [
                f'incident_triage_agent_calls_total{{agent="{agent}"}} {calls}',
                f'incident_triage_agent_success_total{{agent="{agent}"}} {successes}',
                f'incident_triage_agent_failure_total{{agent="{agent}"}} {failures}',
            ]
        )
        for bucket in (0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60):
            lines.append(
                "incident_triage_agent_duration_seconds_bucket"
                f'{{agent="{agent}",le="{bucket}"}} '
                + values.get(prefix + f"duration_bucket:{bucket}", "0")
            )
        lines.extend(
            [
                "incident_triage_agent_duration_seconds_bucket"
                f'{{agent="{agent}",le="+Inf"}} '
                + values.get(prefix + "duration_bucket:+Inf", "0"),
                f'incident_triage_agent_duration_seconds_sum{{agent="{agent}"}} '
                + str(float(values.get(prefix + "duration_ms_sum", "0")) / 1000),
                f'incident_triage_agent_duration_seconds_count{{agent="{agent}"}} '
                + calls,
            ]
        )
    provider_workers = {
        key.split(":")[1]
        for key in values
        if key.startswith("provider:") and key.endswith(":state")
    }
    if provider_workers:
        lines.extend(
            [
                "# HELP incident_triage_provider_circuit_open Whether a worker's provider circuit is open.",
                "# TYPE incident_triage_provider_circuit_open gauge",
                "# HELP incident_triage_provider_circuit_open_total Provider circuit openings per worker.",
                "# TYPE incident_triage_provider_circuit_open_total counter",
            ]
        )
        for worker in sorted(provider_workers):
            safe_worker = "".join(
                character if character.isalnum() or character in "_.-" else "_"
                for character in worker
            )
            prefix = f"provider:{worker}:"
            lines.extend(
                [
                    f'incident_triage_provider_circuit_open{{worker="{safe_worker}"}} '
                    + values.get(prefix + "state", "0"),
                    f'incident_triage_provider_circuit_open_total{{worker="{safe_worker}"}} '
                    + values.get(prefix + "open_count", "0"),
                ]
            )
    return "\n".join(lines) + "\n"


def _index_metrics_text() -> str:
    stats_path = (
        Path(os.getenv("FAISS_INDEX_PATH", "data/faiss")) / "runbooks-stats.json"
    )
    try:
        with stats_path.open(encoding="utf-8") as file:
            stats = json.load(file)
    except FileNotFoundError:
        return (
            "# HELP incident_triage_index_ready Whether a FAISS corpus index is present.\n"
            "# TYPE incident_triage_index_ready gauge\n"
            "incident_triage_index_ready 0\n"
        )
    except json.JSONDecodeError as error:
        logger.error("FAISS index statistics are invalid: %s", error)
        raise HTTPException(
            status_code=503, detail="FAISS index statistics are invalid."
        ) from None

    built_at = stats.get("built_at")
    if not isinstance(built_at, str):
        raise HTTPException(
            status_code=503, detail="FAISS index statistics are invalid."
        )
    try:
        built_timestamp = datetime.fromisoformat(
            built_at.replace("Z", "+00:00")
        ).timestamp()
    except ValueError as error:
        logger.error("FAISS index build timestamp is invalid: %s", error)
        raise HTTPException(
            status_code=503, detail="FAISS index statistics are invalid."
        ) from None
    lines = [
        "# HELP incident_triage_index_ready Whether a FAISS corpus index is present.",
        "# TYPE incident_triage_index_ready gauge",
        "incident_triage_index_ready 1",
        "# HELP incident_triage_index_chunks Number of indexed corpus chunks.",
        "# TYPE incident_triage_index_chunks gauge",
        f"incident_triage_index_chunks {int(stats.get('chunk_count', 0))}",
        "# HELP incident_triage_index_build_timestamp_seconds Unix time of the last index build.",
        "# TYPE incident_triage_index_build_timestamp_seconds gauge",
        f"incident_triage_index_build_timestamp_seconds {built_timestamp}",
        "# HELP incident_triage_index_build_duration_seconds Last index build duration.",
        "# TYPE incident_triage_index_build_duration_seconds gauge",
        f"incident_triage_index_build_duration_seconds {float(stats.get('last_update_duration_seconds', 0.0))}",
        "# HELP incident_triage_index_embed_chunks_per_second Last index embedding throughput.",
        "# TYPE incident_triage_index_embed_chunks_per_second gauge",
        f"incident_triage_index_embed_chunks_per_second {float(stats.get('last_update_chunks_per_second', 0.0))}",
    ]
    source_versions = stats.get("source_versions", [])
    if not isinstance(source_versions, list):
        raise HTTPException(
            status_code=503, detail="FAISS index statistics are invalid."
        )
    if source_versions:
        lines.extend(
            [
                "# HELP incident_triage_index_source_version Information about indexed PostgreSQL documentation versions.",
                "# TYPE incident_triage_index_source_version gauge",
            ]
        )
    for version in source_versions:
        if isinstance(version, str) and version.isdigit():
            lines.append(
                f'incident_triage_index_source_version{{version="{version}"}} 1'
            )
    return "\n".join(lines) + "\n"


@app.post(
    "/incidents/{incident_id}/feedback",
    response_model=FeedbackAccepted,
    status_code=201,
    tags=["feedback"],
)
def submit_feedback(incident_id: UUID, request: FeedbackRequest) -> FeedbackAccepted:
    try:
        feedback_id = store.record_feedback(
            str(incident_id), request.satisfied, request.reason
        )
    except IncidentNotFoundError:
        raise HTTPException(status_code=404, detail="Incident not found.") from None
    except StorageError as error:
        raise HTTPException(status_code=503, detail=str(error)) from None
    return FeedbackAccepted(
        feedback_id=UUID(feedback_id),
        incident_id=incident_id,
    )


@app.get("/incidents/feedback/summary", response_model=FeedbackSummary, tags=["feedback"])
def feedback_summary() -> FeedbackSummary:
    try:
        return FeedbackSummary.model_validate(store.feedback_summary())
    except StorageError as error:
        raise HTTPException(status_code=503, detail=str(error)) from None


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def interface() -> str:
    return _PAGE


_PAGE = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="theme-color" content="#f5f7fb">
  <title>Incident Triage | PostgreSQL Operations</title>
  <style>
    :root {
      color-scheme: light;
      --ink: #17243b;
      --muted: #65738b;
      --line: #e4e9f1;
      --surface: #fff;
      --canvas: #f5f7fb;
      --blue: #315fe8;
      --blue-dark: #254cc4;
      --green: #16835d;
      --amber: #a45b08;
      --red: #b42332;
      --shadow: 0 16px 42px rgba(28, 48, 83, .08);
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      background: var(--canvas);
      color: var(--ink);
      font: 15px/1.55 Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }
    button, input, select, textarea { font: inherit; }
    button, select { cursor: pointer; }
    a { color: var(--blue-dark); }
    .shell { max-width: 1180px; margin: 0 auto; padding: 0 28px 56px; }
    .topbar {
      min-height: 74px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      border-bottom: 1px solid var(--line);
    }
    .brand { display: flex; align-items: center; gap: 12px; font-size: 14px; font-weight: 750; letter-spacing: -.02em; }
    .brand-mark {
      display: grid;
      width: 36px;
      height: 36px;
      place-items: center;
      border-radius: 11px;
      background: #e8edff;
      color: var(--blue);
      font-weight: 850;
    }
    .top-links { display: flex; align-items: center; gap: 20px; color: var(--muted); font-size: 13px; }
    .top-links a { text-decoration: none; }
    .environment { display: inline-flex; align-items: center; gap: 7px; color: #34745d; font-weight: 650; }
    .environment::before { content: ""; width: 7px; height: 7px; border-radius: 50%; background: #36a879; }
    .hero { padding: 46px 0 29px; }
    .eyebrow { color: var(--blue); font-size: 11px; font-weight: 800; letter-spacing: .13em; text-transform: uppercase; }
    h1 { margin: 9px 0 10px; font-size: clamp(30px, 4vw, 43px); letter-spacing: -.045em; line-height: 1.12; }
    .hero-copy { max-width: 680px; margin: 0; color: var(--muted); font-size: 16px; }
    .notice {
      display: flex;
      align-items: flex-start;
      gap: 11px;
      margin-top: 21px;
      padding: 13px 15px;
      border: 1px solid #dfe7fb;
      border-radius: 12px;
      background: #eef3ff;
      color: #435777;
      font-size: 13px;
    }
    .notice strong { color: #253f77; }
    .notice-icon { flex: 0 0 auto; color: var(--blue); font-weight: 850; }
    .observability-card, .performance-card { margin-bottom: 18px; padding: 21px 23px; }
    .observability-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 16px; margin-bottom: 15px; }
    .observability-head h2, .performance-head h2 { margin: 3px 0 0; font-size: 16px; letter-spacing: -.02em; }
    .observability-head p, .performance-head p { margin: 3px 0 0; color: var(--muted); font-size: 12px; }
    .scrape-chip { flex: 0 0 auto; padding: 5px 9px; border: 1px solid #dce8e2; border-radius: 99px; background: #f2faf6; color: #367358; font-size: 10px; font-weight: 750; }
    .monitor-links { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 10px; }
    .monitor-link { min-height: 78px; padding: 12px; border: 1px solid var(--line); border-radius: 10px; background: #fbfcfe; color: #34445e; text-decoration: none; transition: border-color .16s, background .16s, transform .16s; }
    .monitor-link:hover { transform: translateY(-1px); border-color: #b7c8f3; background: #f6f8ff; }
    .monitor-link strong, .monitor-link span { display: block; }
    .monitor-link strong { font-size: 12px; }
    .monitor-link span { margin-top: 3px; color: var(--muted); font-size: 10px; line-height: 1.4; }
    .monitor-note { margin: 12px 0 0; color: #758198; font-size: 11px; }
    .performance-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 14px; margin-bottom: 12px; }
    .recorded-badge { flex: 0 0 auto; padding: 4px 8px; border-radius: 99px; background: #f1f3f7; color: #6d788a; font-size: 10px; font-weight: 750; }
    .table-wrap { overflow-x: auto; }
    .load-table { width: 100%; border-collapse: collapse; text-align: left; white-space: nowrap; }
    .load-table th { padding: 8px 10px; border-bottom: 1px solid var(--line); color: #7b879a; font-size: 10px; font-weight: 750; text-transform: uppercase; letter-spacing: .06em; }
    .load-table td { padding: 10px; border-bottom: 1px solid #edf0f5; color: #43516a; font-size: 12px; }
    .load-table tr:last-child td { border-bottom: 0; }
    .load-table td strong { color: #293951; }
    .slo-met { color: #167451 !important; font-weight: 750; }
    .slo-failed { color: #b42332 !important; font-weight: 750; }
    .recorded-note { margin: 12px 0 0; color: #778398; font-size: 11px; }
    .load-command { margin-top: 12px; padding: 10px 12px; border-radius: 9px; background: #f5f7fb; color: #56647a; font-size: 11px; }
    .load-command code { color: #31415b; font: 11px ui-monospace, SFMono-Regular, Consolas, monospace; overflow-wrap: anywhere; }
    .workspace { display: grid; grid-template-columns: minmax(0, 1.08fr) minmax(300px, .92fr); gap: 20px; align-items: start; }
    .card { border: 1px solid var(--line); border-radius: 16px; background: var(--surface); box-shadow: var(--shadow); }
    .form-card { padding: 23px; }
    .card-heading { display: flex; align-items: flex-start; justify-content: space-between; gap: 15px; margin-bottom: 20px; }
    .card-heading h2, .section-title { margin: 0; font-size: 17px; letter-spacing: -.02em; }
    .subheading { margin: 4px 0 0; color: var(--muted); font-size: 13px; }
    .step { color: #8a96aa; font-size: 12px; font-weight: 700; }
    .field { margin-top: 17px; }
    .field-top { display: flex; justify-content: space-between; align-items: baseline; gap: 12px; margin-bottom: 7px; }
    label { color: #2b3a53; font-size: 13px; font-weight: 700; }
    .hint, .counter { color: #8591a4; font-size: 11px; }
    select, textarea {
      display: block;
      width: 100%;
      border: 1px solid #dce3ed;
      border-radius: 10px;
      background: #fff;
      color: var(--ink);
      outline: none;
      transition: border-color .16s, box-shadow .16s;
    }
    select { min-height: 44px; padding: 0 12px; }
    textarea { min-height: 128px; padding: 12px 13px; resize: vertical; }
    #context { min-height: 106px; font: 12px/1.55 ui-monospace, SFMono-Regular, Consolas, monospace; }
    select:focus, textarea:focus { border-color: #7a98f4; box-shadow: 0 0 0 3px rgba(49, 95, 232, .11); }
    .field-error { min-height: 18px; margin: 5px 0 0; color: var(--red); font-size: 12px; }
    .primary-button {
      display: flex;
      width: 100%;
      min-height: 48px;
      align-items: center;
      justify-content: center;
      gap: 9px;
      margin-top: 16px;
      border: 0;
      border-radius: 10px;
      background: var(--blue);
      color: #fff;
      font-weight: 750;
      box-shadow: 0 6px 14px rgba(49, 95, 232, .2);
      transition: background .16s, transform .16s;
    }
    .primary-button:hover:not(:disabled) { transform: translateY(-1px); background: var(--blue-dark); }
    .primary-button:disabled { cursor: wait; opacity: .7; box-shadow: none; }
    .side-card { padding: 22px; }
    .side-card + .side-card { margin-top: 16px; }
    .side-heading { display: flex; align-items: center; gap: 10px; margin-bottom: 14px; }
    .side-icon { display: grid; width: 32px; height: 32px; place-items: center; border-radius: 9px; background: #eef2ff; color: var(--blue); font-size: 14px; font-weight: 800; }
    .side-heading h2 { margin: 0; font-size: 14px; }
    .workflow-list { display: grid; gap: 13px; margin: 0; padding: 0; list-style: none; }
    .workflow-list li { display: grid; grid-template-columns: 25px 1fr; gap: 10px; color: var(--muted); font-size: 12px; }
    .workflow-number { display: grid; width: 23px; height: 23px; place-items: center; border: 1px solid #e1e7f0; border-radius: 50%; color: #64728a; font-size: 11px; font-weight: 800; }
    .workflow-list strong { display: block; margin-bottom: 1px; color: #30405a; font-size: 12px; }
    .scope-card { background: #fbfcfe; box-shadow: none; }
    .scope-card p { margin: 0; color: var(--muted); font-size: 12px; }
    .scope-card p + p { margin-top: 10px; }
    .result-section { margin-top: 24px; scroll-margin-top: 18px; }
    .result-placeholder, .result-status { padding: 25px; }
    .placeholder-inner { display: flex; align-items: flex-start; gap: 15px; }
    .placeholder-mark { display: grid; flex: 0 0 auto; width: 42px; height: 42px; place-items: center; border-radius: 12px; background: #f0f3f8; color: #71809a; font-size: 19px; }
    .placeholder-inner h2 { margin: 0; font-size: 15px; }
    .placeholder-inner p { margin: 5px 0 0; color: var(--muted); font-size: 13px; }
    .result-status { display: flex; align-items: center; gap: 13px; color: var(--muted); font-size: 13px; }
    .spinner { width: 22px; height: 22px; flex: 0 0 auto; border: 2px solid #dce5fc; border-top-color: var(--blue); border-radius: 50%; animation: spin .75s linear infinite; }
    @keyframes spin { to { transform: rotate(360deg); } }
    .error-card { border-color: #f1d2d5; background: #fffafa; }
    .error-card h2 { color: var(--red); }
    .result-card { overflow: hidden; }
    .result-head { display: flex; flex-wrap: wrap; align-items: flex-start; justify-content: space-between; gap: 14px; padding: 22px 24px; border-bottom: 1px solid var(--line); }
    .result-kicker { color: var(--muted); font-size: 11px; font-weight: 750; letter-spacing: .09em; text-transform: uppercase; }
    .result-head h2 { margin: 4px 0 0; font-size: 21px; letter-spacing: -.025em; }
    .result-subtitle { margin: 3px 0 0; color: var(--muted); font-size: 12px; }
    .badges { display: flex; flex-wrap: wrap; justify-content: flex-end; gap: 7px; }
    .badge { display: inline-flex; align-items: center; gap: 6px; min-height: 27px; padding: 4px 9px; border: 1px solid var(--line); border-radius: 99px; background: #f8f9fc; color: #52617a; font-size: 11px; font-weight: 750; }
    .badge::before { content: ""; width: 6px; height: 6px; border-radius: 50%; background: currentColor; }
    .severity-low, .severity-medium { border-color: #bfe7d6; background: #effaf5; color: #167451; }
    .severity-high { border-color: #f0d7a9; background: #fff8e9; color: #a45b08; }
    .severity-critical { border-color: #f2c5ca; background: #fff0f1; color: #b42332; }
    .verification-ok { border-color: #bfe7d6; background: #effaf5; color: #167451; }
    .verification-failed { border-color: #f2c5ca; background: #fff0f1; color: #b42332; }
    .result-body { padding: 20px 24px 24px; }
    .route-box { display: flex; align-items: flex-start; gap: 11px; padding: 13px 14px; border: 1px solid #dfe7fb; border-radius: 11px; background: #f4f7ff; }
    .route-box .route-icon { color: var(--blue); font-weight: 900; }
    .route-box strong { display: block; color: #2d4475; font-size: 12px; }
    .route-box p { margin: 2px 0 0; color: #60708c; font-size: 12px; }
    .result-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 14px; margin-top: 16px; }
    .result-block { min-width: 0; padding: 15px; border: 1px solid var(--line); border-radius: 11px; }
    .result-block.wide { grid-column: 1 / -1; }
    .result-block h3 { margin: 0 0 9px; color: #394a64; font-size: 12px; letter-spacing: .01em; }
    .result-block ul { display: grid; gap: 8px; margin: 0; padding-left: 18px; color: #43516a; font-size: 12px; }
    .empty-note { margin: 0; color: #8490a2; font-size: 12px; }
    .citation { padding: 12px; border: 1px solid #e8edf4; border-radius: 9px; background: #fbfcfe; }
    .citation + .citation { margin-top: 9px; }
    .citation-title { color: #34445e; font-size: 12px; font-weight: 750; }
    .citation-title a { text-decoration: none; }
    .citation-title a:hover { text-decoration: underline; }
    .citation p { margin: 7px 0 0; color: #66748a; font-size: 12px; white-space: pre-wrap; overflow-wrap: anywhere; }
    .warning { margin-top: 13px; padding: 11px 13px; border: 1px solid #f0d7a9; border-radius: 9px; background: #fff8e9; color: #78551f; font-size: 12px; }
    .technical { margin-top: 15px; border-top: 1px solid var(--line); padding-top: 13px; }
    .technical summary { cursor: pointer; color: #52617a; font-size: 12px; font-weight: 700; }
    .technical pre { max-height: 320px; overflow: auto; margin: 12px 0 0; padding: 13px; border-radius: 9px; background: #f5f7fb; color: #35435b; font: 11px/1.55 ui-monospace, SFMono-Regular, Consolas, monospace; white-space: pre-wrap; overflow-wrap: anywhere; }
    .feedback-card { margin-top: 15px; padding: 20px 22px; }
    .feedback-card h2 { margin: 0; font-size: 15px; }
    .feedback-card > p { margin: 4px 0 13px; color: var(--muted); font-size: 12px; }
    .feedback-row { display: grid; grid-template-columns: minmax(150px, .55fr) 1fr auto; align-items: end; gap: 12px; }
    .feedback-row select { min-height: 40px; }
    .feedback-row textarea { min-height: 40px; height: 40px; resize: vertical; }
    .secondary-button { min-height: 40px; padding: 0 15px; border: 1px solid #d8e0ee; border-radius: 9px; background: #fff; color: #354966; font-size: 12px; font-weight: 750; }
    .secondary-button:hover { border-color: #aebdde; background: #f8faff; }
    .feedback-status { min-height: 18px; margin: 9px 0 0; color: var(--green); font-size: 12px; }
    .feedback-status.error { color: var(--red); }
    .footer { display: flex; justify-content: space-between; gap: 15px; padding-top: 23px; color: #8a95a7; font-size: 11px; }
    .footer a { color: #687792; text-decoration: none; }
    [hidden] { display: none !important; }
    @media (max-width: 760px) {
      .shell { padding: 0 18px 38px; }
      .hero { padding-top: 34px; }
      .monitor-links { grid-template-columns: repeat(2, minmax(0, 1fr)); }
      .workspace { grid-template-columns: 1fr; }
      .result-head { padding: 19px; }
      .result-body { padding: 17px 19px 20px; }
      .badges { justify-content: flex-start; }
    }
    @media (max-width: 520px) {
      .shell { padding-right: 13px; padding-left: 13px; }
      .topbar { min-height: 62px; }
      .top-links { gap: 12px; font-size: 11px; }
      .top-links .environment { display: none; }
      .observability-card, .performance-card { padding: 17px; }
      .observability-head, .performance-head { flex-direction: column; }
      .monitor-links { grid-template-columns: 1fr; }
      .form-card, .side-card { padding: 18px; }
      .result-grid { grid-template-columns: 1fr; }
      .result-block.wide { grid-column: auto; }
      .feedback-row { grid-template-columns: 1fr; }
      .feedback-row .secondary-button { width: 100%; }
      .footer { flex-direction: column; }
    }
    @media (prefers-reduced-motion: reduce) {
      *, *::before, *::after { scroll-behavior: auto !important; animation-duration: .01ms !important; transition-duration: .01ms !important; }
    }
  </style>
</head>
<body>
  <div class="shell">
    <header class="topbar">
      <div class="brand"><span class="brand-mark" aria-hidden="true">IT</span><span>Incident Triage</span></div>
      <nav class="top-links" aria-label="Helpful links">
        <span class="environment">Demo environment</span>
        <a href="/docs">API docs</a>
        <a href="/health">Service health</a>
      </nav>
    </header>

    <main>
      <section class="hero" aria-labelledby="page-title">
        <div class="eyebrow">PostgreSQL operations</div>
        <h1 id="page-title">A clearer first response<br>to database incidents.</h1>
        <p class="hero-copy">Turn an alert and the evidence you have into a grounded assessment, safe next checks, and a clear escalation path.</p>
        <div class="notice" role="note">
          <span class="notice-icon" aria-hidden="true">i</span>
          <span><strong>Advisory only.</strong> Uses synthetic or replayed information. No live systems are connected, and no remediation is executed.</span>
        </div>
      </section>

      <section class="card observability-card" aria-labelledby="observability-title">
        <div class="observability-head">
          <div>
            <div class="eyebrow">Live observability</div>
            <h2 id="observability-title">Monitor the complete triage service</h2>
            <p>Open the operational tools used to inspect service health, agent behavior, and traces.</p>
          </div>
          <span class="scrape-chip">Prometheus scrape interval: 15s</span>
        </div>
        <div class="monitor-links">
          <a class="monitor-link" href="http://localhost:2028/d/incident-triage-overview/incident-triage-service-overview" target="_blank" rel="noopener noreferrer">
            <strong>Grafana dashboard</strong><span>Agent success and latency, retries, API p50/p95, throughput, errors, availability, CPU/RAM, uptime.</span>
          </a>
          <a class="monitor-link" href="http://localhost:2027/targets" target="_blank" rel="noopener noreferrer">
            <strong>Prometheus targets</strong><span>Check scrape health for the configured application and infrastructure targets.</span>
          </a>
          <a class="monitor-link" href="http://localhost:2030" target="_blank" rel="noopener noreferrer">
            <strong>Jaeger traces</strong><span>Follow a correlated request through API, worker, agents, and tool calls.</span>
          </a>
          <a class="monitor-link" href="/metrics" target="_blank" rel="noopener noreferrer">
            <strong>Raw API metrics</strong><span>Inspect Prometheus-format API and Redis-backed worker metrics.</span>
          </a>
        </div>
        <p class="monitor-note"><strong>Metric sources:</strong> worker and per-agent counters are shared through Redis and exported by the API; cAdvisor supplies container CPU, memory, and uptime. The worker does not expose a separate Prometheus HTTP endpoint.</p>
      </section>

      <section class="card performance-card" aria-labelledby="load-test-title">
        <div class="performance-head">
          <div>
            <div class="eyebrow">Performance demonstration · D6</div>
            <h2 id="load-test-title">Recorded Locust baseline</h2>
            <p>Complete-run measurements include queue wait and worker execution.</p>
          </div>
          <span class="recorded-badge">Recorded results · not live</span>
        </div>
        <div class="table-wrap">
          <table class="load-table">
            <thead><tr><th>Workers</th><th>Users</th><th>Completed</th><th>RPS</th><th>p50</th><th>p95</th><th>SLO ≤ 30s</th></tr></thead>
            <tbody>
              <tr><td>1</td><td>10</td><td>25</td><td>0.423</td><td>23.0s</td><td>23.0s</td><td class="slo-met">Met</td></tr>
              <tr><td>3</td><td>10</td><td>72</td><td>1.212</td><td>7.9s</td><td>8.9s</td><td class="slo-met">Met</td></tr>
              <tr><td>1</td><td>15</td><td>24</td><td>0.411</td><td>28.0s</td><td>36.0s</td><td class="slo-failed">Failed</td></tr>
            </tbody>
          </table>
        </div>
        <p class="recorded-note">At 10 users, three workers delivered about 2.9x throughput and 61% lower p95. The first tested failure was 15 users with one worker. These results used deterministic mode and a fixed 1s worker delay; they are not Groq-provider latency.</p>
        <div class="load-command">To repeat the scale comparison, keep the same Locust workload and delay, and change replicas with <code>docker compose up --build -d --scale agent-worker=3</code>. Run details: <code>docs/evaluation.md</code>.</div>
      </section>

      <section class="workspace" aria-label="Incident submission">
        <form id="triage-form" class="card form-card">
          <div class="card-heading">
            <div>
              <h2>Start an assessment</h2>
              <p class="subheading">Describe the symptom and share any known metrics.</p>
            </div>
            <span class="step">01 / INTAKE</span>
          </div>
          <div class="field">
            <div class="field-top"><label for="sample-alert">Synthetic Alert Generator</label><span class="hint">Optional starter scenario</span></div>
            <select id="sample-alert">
              <option value="">Choose a sample alert</option>
              <option value="latency">High database latency</option>
              <option value="connections">Connection pool nearly exhausted</option>
              <option value="disk">Disk usage increasing</option>
            </select>
          </div>
          <div class="field">
            <div class="field-top"><label for="description">Incident description</label><span id="description-count" class="counter">0 / 10,000</span></div>
            <textarea id="description" maxlength="10000" placeholder="What are you observing? Include impact, timing, and what has changed." required aria-describedby="description-error"></textarea>
            <p id="description-error" class="field-error" aria-live="polite"></p>
          </div>
          <div class="field">
            <div class="field-top"><label for="context">Additional context</label><span class="hint">Optional JSON object</span></div>
            <textarea id="context" spellcheck="false" placeholder='{"disk_used_percent": 88, "disk_growth": "increasing", "table_index_sizes": "stable"}' aria-describedby="context-help context-error"></textarea>
            <p id="context-help" class="hint">Use only supported context fields. Values are treated as reported evidence.</p>
            <p id="context-error" class="field-error" aria-live="polite"></p>
          </div>
          <button id="submit-button" class="primary-button" type="submit"><span aria-hidden="true">✦</span><span>Analyze incident</span></button>
        </form>

        <aside aria-label="How this works">
          <section class="card side-card">
            <div class="side-heading"><span class="side-icon" aria-hidden="true">↗</span><h2>What happens next</h2></div>
            <ol class="workflow-list">
              <li><span class="workflow-number">1</span><span><strong>Review the supplied evidence</strong>The request is validated before any analysis starts.</span></li>
              <li><span class="workflow-number">2</span><span><strong>Find relevant runbook guidance</strong>The investigation searches the supported PostgreSQL runbooks.</span></li>
              <li><span class="workflow-number">3</span><span><strong>Verify before recommending</strong>Sources are checked; unsupported findings are escalated.</span></li>
            </ol>
          </section>
          <section class="card side-card scope-card">
            <div class="side-heading"><span class="side-icon" aria-hidden="true">✓</span><h2>Designed for safe triage</h2></div>
            <p><strong>Evidence, not guesses.</strong> Observed facts and possible causes are kept separate.</p>
            <p><strong>Human in control.</strong> Recommendations are advisory; destructive actions are not performed.</p>
            <p><strong>Limited scope.</strong> Latency, connection exhaustion, and rising disk usage / possible WAL retention.</p>
          </section>
        </aside>
      </section>

      <section id="assessment" class="result-section" aria-live="polite" aria-busy="false" aria-label="Triage assessment">
        <div id="result-placeholder" class="card result-placeholder">
          <div class="placeholder-inner">
            <span class="placeholder-mark" aria-hidden="true">◎</span>
            <div><h2>Your assessment will appear here</h2><p>Submit an incident to see its severity, evidence, runbook sources, and recommended next checks.</p></div>
          </div>
        </div>
        <div id="result-status" class="card result-status" role="status" hidden><span class="spinner" aria-hidden="true"></span><span id="status-message">Preparing your assessment…</span></div>
        <div id="result-error" class="card result-placeholder error-card" role="alert" hidden>
          <div class="placeholder-inner"><span class="placeholder-mark" aria-hidden="true">!</span><div><h2 id="error-title">We could not complete the assessment</h2><p id="error-message"></p></div></div>
        </div>
        <article id="result-card" class="card result-card" hidden></article>
        <form id="feedback-form" class="card feedback-card" hidden>
          <h2>Was this assessment useful?</h2>
          <p>Your feedback is saved with the incident and helps evaluate the triage experience.</p>
          <div class="feedback-row">
            <div><label for="feedback-satisfied">Your assessment</label><select id="feedback-satisfied"><option value="true">Yes, it was useful</option><option value="false">No, it needs improvement</option></select></div>
            <div><label for="feedback-reason">Reason <span class="hint">(required for “No”)</span></label><textarea id="feedback-reason" maxlength="1000" placeholder="What was missing or incorrect?"></textarea></div>
            <button id="feedback-button" class="secondary-button" type="submit">Send feedback</button>
          </div>
          <p id="feedback-status" class="feedback-status" role="status"></p>
        </form>
      </section>
    </main>

    <footer class="footer"><span>PostgreSQL Incident Triage Assistant</span><span>For local evaluation only · <a href="/docs">Explore the API</a></span></footer>
  </div>
  <script>
    const sampleAlerts = {
      latency: {
        description: "Synthetic alert: database latency is elevated.",
        context: { observed_latency_ms: 1800, threshold_latency_ms: 500 }
      },
      connections: {
        description: "Synthetic alert: the database connection pool is nearly exhausted.",
        context: { current_connections: 96, max_connections: 100 }
      },
      disk: {
        description: "Synthetic alert: disk usage is increasing while table and index sizes are stable.",
        context: { disk_used_percent: 88, disk_growth: "increasing", table_index_sizes: "stable" }
      }
    };
    const form = document.getElementById("triage-form");
    const submitButton = document.getElementById("submit-button");
    const description = document.getElementById("description");
    const contextInput = document.getElementById("context");
    const assessment = document.getElementById("assessment");
    const placeholder = document.getElementById("result-placeholder");
    const statusPanel = document.getElementById("result-status");
    const resultError = document.getElementById("result-error");
    const resultCard = document.getElementById("result-card");
    const feedbackForm = document.getElementById("feedback-form");
    const feedbackStatus = document.getElementById("feedback-status");

    function makeElement(tag, className, text) {
      const node = document.createElement(tag);
      if (className) node.className = className;
      if (text !== undefined && text !== null) node.textContent = String(text);
      return node;
    }
    function appendList(block, title, items, emptyText) {
      const heading = makeElement("h3", "", title);
      block.append(heading);
      if (!items || !items.length) {
        block.append(makeElement("p", "empty-note", emptyText));
        return;
      }
      const list = makeElement("ul");
      items.forEach((item) => list.append(makeElement("li", "", item)));
      block.append(list);
    }
    function makeBlock(title, items, emptyText, wide) {
      const block = makeElement("section", `result-block${wide ? " wide" : ""}`);
      appendList(block, title, items, emptyText);
      return block;
    }
    function resetResult() {
      placeholder.hidden = true;
      resultError.hidden = true;
      resultCard.hidden = true;
      resultCard.replaceChildren();
      feedbackForm.hidden = true;
      feedbackStatus.textContent = "";
      assessment.setAttribute("aria-busy", "true");
    }
    function showFailure(title, message) {
      statusPanel.hidden = true;
      assessment.setAttribute("aria-busy", "false");
      document.getElementById("error-title").textContent = title;
      document.getElementById("error-message").textContent = message;
      resultError.hidden = false;
    }
    function showTechnicalDetails(result) {
      const details = makeElement("details", "technical");
      details.append(makeElement("summary", "", "Technical details and agent trace"));
      const pre = makeElement("pre");
      pre.textContent = JSON.stringify({
        cause_class: result.cause_class,
        confidence: result.confidence,
        agent_steps: result.agent_steps,
        agent_timings: result.agent_timings,
        retrieval_retries: result.retrieval_retries,
        tool_calls: result.tool_calls,
        prompt_versions: result.prompt_versions,
        model_usage: result.model_usage,
        estimated_cost_usd: result.estimated_cost_usd
      }, null, 2);
      details.append(pre);
      return details;
    }
    function renderResult(result, runId) {
      statusPanel.hidden = true;
      assessment.setAttribute("aria-busy", "false");
      resultCard.replaceChildren();
      const categoryNames = {
        high_database_latency: "High database latency",
        too_many_connections: "Too many connections",
        disk_usage_wal_retention: "Disk usage / possible WAL retention",
        unknown: "Unknown incident"
      };
      const category = categoryNames[result.category] || "Incident assessment";
      const head = makeElement("header", "result-head");
      const titleWrap = makeElement("div");
      titleWrap.append(makeElement("div", "result-kicker", `Assessment · Run ${runId}`));
      titleWrap.append(makeElement("h2", "", category));
      titleWrap.append(makeElement("p", "result-subtitle", result.cause_class ? `Cause class: ${result.cause_class.replaceAll("_", " ")}` : "Based on the submitted incident details"));
      const badges = makeElement("div", "badges");
      const severity = String(result.severity || "unknown").toLowerCase();
      badges.append(makeElement("span", `badge severity-${severity}`, `${severity.charAt(0).toUpperCase()}${severity.slice(1)} severity`));
      badges.append(makeElement("span", `badge ${result.verification_passed ? "verification-ok" : "verification-failed"}`, result.verification_passed ? "Sources verified" : "Verification failed"));
      head.append(titleWrap, badges);

      const body = makeElement("div", "result-body");
      const route = makeElement("div", "route-box");
      route.append(makeElement("span", "route-icon", result.route === "incident-commander" ? "!" : "→"));
      const routeText = makeElement("div");
      routeText.append(makeElement("strong", "", `Recommended route: ${result.route || "human review"}`));
      routeText.append(makeElement("p", "", result.routing_basis || "Route selected from the project severity policy."));
      route.append(routeText);
      body.append(route);

      if (result.rag_summary) {
        const summary = makeElement("section", "result-block wide");
        summary.append(makeElement("h3", "", "Grounded summary"));
        summary.append(makeElement("p", "empty-note", result.rag_summary));
        const summaryGrid = makeElement("div", "result-grid");
        summaryGrid.append(summary);
        body.append(summaryGrid);
      }
      const grid = makeElement("div", "result-grid");
      grid.append(makeBlock("Observed evidence", result.evidence_observed, "No evidence was supplied or collected."));
      grid.append(makeBlock("Possible explanations", result.hypotheses, "No cause hypothesis was established."));
      grid.append(makeBlock("Recommended next checks", result.checks_recommended, "No additional checks were recommended."));
      grid.append(makeBlock("Checks performed", result.checks_performed, "No live diagnostic checks were performed."));
      grid.append(makeBlock("Recommended actions", result.recommended_actions, "No action recommendation is available.", true));

      const sources = makeElement("section", "result-block wide");
      sources.append(makeElement("h3", "", "Runbook sources"));
      const references = result.runbook_references || [];
      if (!references.length) {
        sources.append(makeElement("p", "empty-note", "No supporting runbook source was found. Use the escalation guidance and request more evidence."));
      } else {
        references.forEach((reference) => {
          const citation = makeElement("div", "citation");
          const citationTitle = makeElement("div", "citation-title");
          const label = [reference.filename, reference.heading].filter(Boolean).join(" · ");
          if (reference.source_url) {
            try {
              const url = new URL(reference.source_url, window.location.href);
              if (url.protocol === "https:" || url.protocol === "http:") {
                const link = makeElement("a", "", label);
                link.href = url.href;
                link.target = "_blank";
                link.rel = "noopener noreferrer";
                citationTitle.append(link);
              } else {
                citationTitle.textContent = label;
              }
            } catch {
              citationTitle.textContent = label;
            }
          } else {
            citationTitle.textContent = label;
          }
          citation.append(citationTitle);
          if (reference.excerpt) citation.append(makeElement("p", "", reference.excerpt));
          sources.append(citation);
        });
      }
      grid.append(sources);
      grid.append(makeBlock("Uncertainties", result.uncertainties, "No additional uncertainty was recorded.", true));
      const escalation = makeElement("section", "result-block wide");
      escalation.append(makeElement("h3", "", "Escalation guidance"));
      escalation.append(makeElement("p", "empty-note", result.escalation_guidance || "Follow your organization's incident process."));
      grid.append(escalation);
      body.append(grid);
      if (result.rag_warning || (result.safety_flags && result.safety_flags.length)) {
        const warningText = [result.rag_warning, ...(result.safety_flags || [])].filter(Boolean).join(" · ");
        body.append(makeElement("div", "warning", warningText));
      }
      body.append(showTechnicalDetails(result));
      resultCard.append(head, body);
      resultCard.hidden = false;
      feedbackForm.dataset.incidentId = result.incident_id || "";
      feedbackForm.hidden = !result.incident_id;
    }

    document.getElementById("sample-alert").addEventListener("change", (event) => {
      const sample = sampleAlerts[event.target.value];
      if (sample) {
        description.value = sample.description;
        contextInput.value = JSON.stringify(sample.context, null, 2);
        document.getElementById("description-error").textContent = "";
        document.getElementById("context-error").textContent = "";
        description.dispatchEvent(new Event("input"));
      }
    });
    description.addEventListener("input", () => {
      document.getElementById("description-count").textContent = `${description.value.length.toLocaleString()} / 10,000`;
    });
    document.getElementById("feedback-satisfied").addEventListener("change", (event) => {
      const reason = document.getElementById("feedback-reason");
      reason.required = event.target.value === "false";
      if (!reason.required) reason.setCustomValidity("");
    });
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      document.getElementById("description-error").textContent = "";
      document.getElementById("context-error").textContent = "";
      const incidentDescription = description.value.trim();
      if (!incidentDescription) {
        document.getElementById("description-error").textContent = "Enter a short description of the incident.";
        description.focus();
        return;
      }
      let context = null;
      const contextText = contextInput.value.trim();
      if (contextText) {
        try {
          context = JSON.parse(contextText);
          if (!context || Array.isArray(context) || typeof context !== "object") {
            throw new Error("Context must be a JSON object.");
          }
        } catch (error) {
          document.getElementById("context-error").textContent = error instanceof SyntaxError ? "Check the JSON syntax and try again." : error.message;
          contextInput.focus();
          return;
        }
      }
      resetResult();
      statusPanel.hidden = false;
      document.getElementById("status-message").textContent = "Submitting incident for analysis…";
      submitButton.disabled = true;
      submitButton.lastElementChild.textContent = "Analyzing…";
      assessment.scrollIntoView({ behavior: "smooth", block: "start" });
      try {
        const response = await fetch("/incidents/triage", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            incident_description: incidentDescription,
            context
          })
        });
        const body = await response.json().catch(() => ({}));
        if (!response.ok) {
          const message = body.detail
            ? (typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail, null, 2))
            : "The request was rejected. Check the incident details and try again.";
          showFailure("The request could not be accepted", message);
          return;
        }
        document.getElementById("status-message").textContent = `Run ${body.run_id} is ${body.status}. Waiting for the worker…`;
        const deadline = Date.now() + 180000;
        while (Date.now() < deadline) {
          await new Promise((resolve) => setTimeout(resolve, 1000));
          const statusResponse = await fetch(body.status_url);
          const statusBody = await statusResponse.json().catch(() => ({}));
          if (!statusResponse.ok) {
            showFailure("Could not retrieve the run status", statusBody.detail || "The status service returned an error. You can try submitting the incident again.");
            return;
          }
          if (statusBody.status === "finished") {
            renderResult(statusBody.result, body.run_id);
            return;
          }
          if (statusBody.status === "failed") {
            showFailure("The triage run failed", statusBody.error || "The worker could not complete this assessment. No recommendation was returned.");
            return;
          }
          document.getElementById("status-message").textContent = `Run ${body.run_id} is ${statusBody.status || "processing"}. Waiting for the worker…`;
        }
        showFailure("This assessment is taking longer than expected", `Run ${body.run_id} has not completed yet. Check the service status or submit a new request later.`);
      } catch (error) {
        showFailure("Could not connect to the triage service", "Check that the service is running, then try again.");
      } finally {
        submitButton.disabled = false;
        submitButton.lastElementChild.textContent = "Analyze incident";
      }
    });
    feedbackForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      const reason = document.getElementById("feedback-reason");
      const satisfied = document.getElementById("feedback-satisfied").value === "true";
      if (!satisfied && !reason.value.trim()) {
        reason.setCustomValidity("Please add a reason for your feedback.");
        reason.reportValidity();
        return;
      }
      reason.setCustomValidity("");
      const feedbackButton = document.getElementById("feedback-button");
      feedbackButton.disabled = true;
      const body = {
        satisfied,
        reason: reason.value.trim()
      };
      try {
        const response = await fetch(`/incidents/${feedbackForm.dataset.incidentId}/feedback`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body)
        });
        const result = await response.json().catch(() => ({}));
        feedbackStatus.className = `feedback-status${response.ok ? "" : " error"}`;
        feedbackStatus.textContent = response.ok ? "Feedback recorded." : `Feedback error: ${typeof result.detail === "string" ? result.detail : "Please try again."}`;
      } catch {
        feedbackStatus.className = "feedback-status error";
        feedbackStatus.textContent = "Could not submit feedback.";
      } finally {
        feedbackButton.disabled = false;
      }
    });
  </script>
</body>
</html>"""
