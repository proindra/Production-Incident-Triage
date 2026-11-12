"""FastAPI routes and the minimal browser-based triage interface."""

from __future__ import annotations

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse

from app.config import DEFAULT_SETTINGS
from app.diagnostics.mock import MockDiagnostic
from app.diagnostics.postgres_readonly import PostgresReadOnlyDiagnostic
from app.schemas import HealthResponse, TriageRequest, TriageResponse
from app.storage.incidents import IncidentStore, StorageError
from app.triage import TriageEngine

diagnostic = (
    PostgresReadOnlyDiagnostic(DEFAULT_SETTINGS.database_url)
    if DEFAULT_SETTINGS.database_url
    else MockDiagnostic()
)
engine = TriageEngine(
    diagnostic=diagnostic,
)
store = IncidentStore(DEFAULT_SETTINGS.incident_db_path)
app = FastAPI(title="Incident Runbook Triage Assistant", version="0.1.0")


@app.get("/health", response_model=HealthResponse, tags=["health"])
def health() -> HealthResponse:
    return HealthResponse(status="ok")


@app.post("/incidents/triage", response_model=TriageResponse, tags=["triage"])
def triage(request: TriageRequest) -> TriageResponse:
    result = engine.triage(request)
    try:
        result.incident_id = store.record(result)
    except StorageError as error:
        raise HTTPException(status_code=503, detail=str(error)) from None
    return result


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
        output.textContent = response.ok ? JSON.stringify(body, null, 2) : (body.detail || "Request failed.");
      } catch (error) {
        output.textContent = "Could not submit the incident. Check the context JSON and service connection.";
      }
    });
  </script>
</body>
</html>"""
