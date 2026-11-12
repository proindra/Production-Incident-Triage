from app.diagnostics.mock import MockDiagnostic
from app.schemas import TriageRequest
from app.triage import TriageEngine


def test_latency_uses_reported_metrics_and_explicit_mock_checks() -> None:
    diagnostic = MockDiagnostic(
        {
            "high_database_latency": {
                "active_sessions": [{"state": "active", "wait_event_type": "Lock"}],
                "_checks_performed": ["active_sessions"],
            }
        }
    )
    result = TriageEngine(diagnostic=diagnostic).triage(
        TriageRequest(
            incident_description="High database latency is affecting requests",
            context={"observed_latency_ms": 2000, "threshold_latency_ms": 500},
        )
    )

    assert result.category == "high_database_latency"
    assert result.severity == "high"
    assert result.checks_performed == ["active_sessions"]
    assert any("Diagnostic result" in item for item in result.evidence_observed)
    assert result.runbook_references[0].filename == "high_database_latency.md"
    assert any("cause" in item for item in result.hypotheses)
    assert all("terminate" not in action.lower() or "do not" in action.lower() for action in result.recommended_actions)


def test_latency_without_live_data_reports_uncertainty() -> None:
    result = TriageEngine().triage(TriageRequest(incident_description="Database latency has increased"))

    assert result.checks_performed == []
    assert any("No live database checks" in item for item in result.uncertainties)
    assert result.checks_recommended
