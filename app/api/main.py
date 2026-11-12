"""FastAPI routes and the minimal browser-based triage interface."""

from __future__ import annotations

import json
import logging
import time
import uuid

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest
from opentelemetry import propagate, trace
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from uuid import UUID

from app.core.config import DEFAULT_SETTINGS
from app.core.observability import configure_observability
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
    output = generate_latest().decode("utf-8") + _worker_metrics_text(worker_metrics)
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
  <title>Incident Runbook Triage</title>
  <style>
    body { font: 16px system-ui, sans-serif; max-width: 860px; margin: 3rem auto; padding: 0 1rem; color: #172033; }
    textarea, button { box-sizing: border-box; width: 100%; margin: .5rem 0 1rem; padding: .8rem; font: inherit; }
    textarea { min-height: 7rem; } button { cursor: pointer; background: #174ea6; color: white; border: 0; border-radius: .3rem; }
    pre { white-space: pre-wrap; overflow-wrap: anywhere; background: #f3f5f8; padding: 1rem; }
  </style>
</head>
<body>
  <h1>Incident Runbook Triage</h1>
  <p>Advisory-only PostgreSQL triage. No remediation is executed.</p>
  <form id="triage-form">
    <label for="description">Incident description</label>
    <textarea id="description" maxlength="10000" required></textarea>
    <label for="context">Optional context (JSON object)</label>
    <textarea id="context" placeholder='{"disk_used_percent": 88, "disk_growth": "increasing", "table_index_sizes": "stable"}'></textarea>
    <button type="submit">Analyze incident</button>
  </form>
  <h2>Assessment</h2>
  <pre id="result" aria-live="polite">Submit an incident to see its evidence, uncertainty, runbooks, and safe next checks.</pre>
  <form id="feedback-form" hidden>
    <h2>Was this assessment satisfactory?</h2>
    <label for="feedback-satisfied">Assessment</label>
    <select id="feedback-satisfied">
      <option value="true">Yes</option>
      <option value="false">No</option>
    </select>
    <label for="feedback-reason">Reason (required for “No”)</label>
    <textarea id="feedback-reason" maxlength="1000"></textarea>
    <button type="submit">Submit feedback</button>
  </form>
  <script>
    document.getElementById("triage-form").addEventListener("submit", async (event) => {
      event.preventDefault();
      const output = document.getElementById("result");
      try {
        const contextText = document.getElementById("context").value.trim();
        const context = contextText ? JSON.parse(contextText) : null;
        const response = await fetch("/incidents/triage", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            incident_description: document.getElementById("description").value,
            context
          })
        });
        const body = await response.json();
        if (!response.ok) {
          output.textContent = JSON.stringify(body, null, 2);
          return;
        }
        output.textContent = `Run ${body.run_id}: ${body.status}`;
        while (true) {
          await new Promise((resolve) => setTimeout(resolve, 1000));
          const statusResponse = await fetch(body.status_url);
          const statusBody = await statusResponse.json();
          if (!statusResponse.ok) {
            output.textContent = JSON.stringify(statusBody, null, 2);
            return;
          }
          if (statusBody.status === "finished") {
            output.textContent = JSON.stringify(statusBody.result, null, 2);
            const feedbackForm = document.getElementById("feedback-form");
            feedbackForm.dataset.incidentId = statusBody.result.incident_id;
            feedbackForm.hidden = !statusBody.result.incident_id;
            return;
          }
          if (statusBody.status === "failed") {
            output.textContent = statusBody.error || "The triage run failed.";
            return;
          }
          output.textContent = `Run ${body.run_id}: ${statusBody.status}`;
        }
      } catch (error) {
        output.textContent = "Could not submit the incident. Check the context JSON and service connection.";
      }
    });
    document.getElementById("feedback-form").addEventListener("submit", async (event) => {
      event.preventDefault();
      const form = event.currentTarget;
      const body = {
        satisfied: document.getElementById("feedback-satisfied").value === "true",
        reason: document.getElementById("feedback-reason").value
      };
      try {
        const response = await fetch(`/incidents/${form.dataset.incidentId}/feedback`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body)
        });
        const result = await response.json();
        document.getElementById("result").textContent +=
          response.ok ? "\nFeedback recorded." : `\nFeedback error: ${JSON.stringify(result)}`;
      } catch {
        document.getElementById("result").textContent += "\nCould not submit feedback.";
      }
    });
  </script>
</body>
</html>"""
