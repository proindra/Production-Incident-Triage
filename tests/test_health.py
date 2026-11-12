import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.main import app
from app.schemas import TriageRequest, TriageResponse

client = TestClient(app)


def test_health_returns_ok() -> None:
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_triage_endpoint_returns_assessment_and_persists_history() -> None:
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

    assert response.status_code == 200
    body = response.json()
    assert body["incident_id"]
    assert body["category"] == "disk_usage_wal_retention"
    assert body["severity"] == "medium"
    assert body["runbook_references"][0]["filename"] == "disk_usage_wal_retention.md"
    assert body["checks_performed"] == []


def test_triage_endpoint_rejects_blank_description() -> None:
    response = client.post(
        "/incidents/triage",
        json={"incident_description": "   "},
    )

    assert response.status_code == 422


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
        context={"active_connections": 48},
    )

    assert request.context == {"active_connections": 48}


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
