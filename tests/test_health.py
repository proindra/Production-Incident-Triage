import pytest
import uuid
from fastapi.testclient import TestClient
from pydantic import ValidationError

import app.api.main as main
from app.main import app
from app.domain.schemas import TriageRequest, TriageResponse
from app.infrastructure.storage.incidents import IncidentStore
from app.services.triage import TriageEngine

client = TestClient(app)


class ImmediateTestQueue:
    def __init__(self) -> None:
        self.jobs = {}

    def ping(self) -> bool:
        return True

    def allow_request(self, client_id: str, limit: int = 30, window_seconds: int = 60) -> bool:
        return True

    def enqueue(self, request: dict, correlation_id: str, trace_context=None) -> str:
        run_id = str(uuid.uuid4())
        result = TriageEngine().triage(TriageRequest.model_validate(request))
        self.jobs[run_id] = {
            "run_id": run_id,
            "status": "finished",
            "result": result.model_dump(mode="json"),
            "error": None,
        }
        return run_id

    def status(self, run_id: str):
        return self.jobs.get(run_id)


def test_health_returns_ok() -> None:
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_readiness_checks_queue_dependency(monkeypatch) -> None:
    class UnavailableQueue:
        def ping(self):
            raise main.QueueError("The Redis task queue is unavailable.")

    monkeypatch.setattr(main, "task_queue", UnavailableQueue())

    response = client.get("/ready")

    assert response.status_code == 503


def test_triage_endpoint_returns_pollable_run(monkeypatch) -> None:
    monkeypatch.setattr(main, "task_queue", ImmediateTestQueue())
    response = client.post(
        "/incidents/triage",
        json={
            "incident_description": "Disk usage increasing while table and index sizes are stable",
            "context": {
                "disk_used_percent": 88,
                "disk_growth": "increasing",
                "table_index_sizes": "stable",
            },
        },
    )

    assert response.status_code == 202
    body = response.json()
    assert body["run_id"]
    assert body["status"] == "queued"
    status = client.get(body["status_url"])
    assert status.status_code == 200
    assert status.json()["result"]["category"] == "disk_usage_wal_retention"
    assert status.json()["result"]["severity"] == "medium"
    assert status.json()["result"]["runbook_references"][0]["filename"] == "disk_usage_wal_retention.md"
    assert status.json()["result"]["verification_passed"] is True


def test_triage_endpoint_rejects_blank_description() -> None:
    response = client.post(
        "/incidents/triage",
        json={"incident_description": "   "},
    )

    assert response.status_code == 422


def test_invalid_request_is_quarantined_without_queueing(tmp_path, monkeypatch) -> None:
    class CountingQueue:
        enqueued = False

        def ping(self):
            return True

        def allow_request(self, client_id, limit=30, window_seconds=60):
            return True

        def enqueue(self, request, correlation_id, trace_context=None):
            self.enqueued = True
            raise AssertionError("Invalid input must not be queued.")

        def status(self, run_id):
            return None

    queue = CountingQueue()
    store = IncidentStore(tmp_path / "incidents.sqlite")
    monkeypatch.setattr(main, "task_queue", queue)
    monkeypatch.setattr(main, "store", store)

    response = client.post(
        "/incidents/triage",
        json={"incident_description": "Disk is filling", "context": []},
    )

    assert response.status_code == 422
    assert response.json()["quarantine_id"]
    assert queue.enqueued is False
    import sqlite3

    with sqlite3.connect(store.database_path) as connection:
        row = connection.execute(
            "SELECT field_errors FROM quarantined_requests"
        ).fetchone()
    assert row is not None
    assert "body.context" in row[0]


def test_ui_page_loads() -> None:
    response = client.get("/")

    assert response.status_code == 200
    assert "Incident Runbook Triage" in response.text


def test_triage_request_requires_non_blank_incident_description() -> None:
    with pytest.raises(ValidationError):
        TriageRequest(incident_description="   ")


def test_triage_request_limits_context_fields() -> None:
    with pytest.raises(ValidationError):
        TriageRequest(incident_description="Database issue", context={str(i): i for i in range(101)})


def test_triage_request_accepts_optional_context() -> None:
    request = TriageRequest(
        incident_description="Database latency has increased",
        context={"current_connections": 48, "max_connections": 100},
    )

    assert request.context == {"current_connections": 48, "max_connections": 100}


def test_alert_payload_and_on_call_note_form_the_incident_description() -> None:
    request = TriageRequest(
        alert={
            "incident_type": "disk_usage_increasing",
            "database": "app_db",
            "metrics": {"disk_used_percent": 88, "disk_growth": "increasing"},
        },
        on_call_note="Table and index sizes are stable.",
    )

    assert "disk usage increasing" in request.incident_description
    assert "Table and index sizes are stable." in request.incident_description
    assert request.context == {"disk_used_percent": 88, "disk_growth": "increasing"}


def test_alert_contract_rejects_out_of_range_metrics_and_unpaired_connection_counts() -> None:
    with pytest.raises(ValidationError):
        TriageRequest(
            incident_description="Disk is filling",
            context={"disk_used_percent": 101},
        )
    with pytest.raises(ValidationError):
        TriageRequest(
            incident_description="Connections rising",
            context={"current_connections": 10},
        )


def test_feedback_is_persisted_and_aggregated(tmp_path, monkeypatch) -> None:
    store = IncidentStore(tmp_path / "feedback.sqlite")
    result = TriageEngine().triage(
        TriageRequest(incident_description="Database latency has increased")
    )
    incident_id = store.record(result)
    monkeypatch.setattr(main, "store", store)

    response = client.post(
        f"/incidents/{incident_id}/feedback",
        json={"satisfied": False, "reason": "The suggested checks were incomplete."},
    )

    assert response.status_code == 201
    summary = client.get("/incidents/feedback/summary")
    assert summary.json() == {
        "total": 1,
        "satisfied": 0,
        "unsatisfied": 1,
        "satisfaction_rate": 0.0,
    }


def test_negative_feedback_requires_a_reason() -> None:
    response = client.post(
        "/incidents/00000000-0000-0000-0000-000000000000/feedback",
        json={"satisfied": False},
    )

    assert response.status_code == 422


def test_submission_rate_limit_is_per_client(monkeypatch) -> None:
    class RateLimitedQueue(ImmediateTestQueue):
        requests = {}

        def allow_request(self, client_id: str, limit: int = 30, window_seconds: int = 60) -> bool:
            self.requests[client_id] = self.requests.get(client_id, 0) + 1
            return self.requests[client_id] <= 1

    monkeypatch.setattr(main, "task_queue", RateLimitedQueue())
    payload = {"incident_description": "Database latency is elevated"}

    first = client.post("/incidents/triage", json=payload)
    second = client.post("/incidents/triage", json=payload)

    assert first.status_code == 202
    assert second.status_code == 429


def test_triage_response_validates_confidence_and_separates_facts() -> None:
    response = TriageResponse(
        category="high_database_latency",
        severity="medium",
        confidence=0.7,
        evidence_observed=["Latency increased according to the supplied report."],
        hypotheses=["A lock wait may be contributing."],
        checks_performed=[],
        checks_recommended=["Inspect PostgreSQL wait events."],
        runbook_references=[],
        recommended_actions=["Collect wait-event data before changing configuration."],
        escalation_guidance="Escalate if service availability is affected.",
        uncertainties=["No live database checks have been run."],
    )

    assert response.checks_performed == []
    assert response.hypotheses != response.evidence_observed

    with pytest.raises(ValidationError):
        TriageResponse(
            category="unknown",
            severity="unknown",
            confidence=1.1,
            evidence_observed=[],
            hypotheses=[],
            checks_performed=[],
            checks_recommended=[],
            runbook_references=[],
            recommended_actions=[],
            escalation_guidance="",
            uncertainties=[],
        )
