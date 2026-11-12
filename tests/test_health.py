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


def test_triage_request_requires_non_blank_incident_description() -> None:
    with pytest.raises(ValidationError):
        TriageRequest(incident_description="   ")


def test_triage_request_accepts_optional_context() -> None:
    request = TriageRequest(
        incident_description="Database latency has increased",
        context={"active_connections": 48},
    )

    assert request.context == {"active_connections": 48}


def test_triage_response_validates_confidence_and_separates_facts() -> None:
    response = TriageResponse(
        category="database_latency",
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
