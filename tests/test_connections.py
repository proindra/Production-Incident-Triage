from app.schemas import TriageRequest
from app.triage import TriageEngine


def test_connection_counts_are_reported_from_supplied_context() -> None:
    result = TriageEngine().triage(
        TriageRequest(
            incident_description="Too many database connections",
            context={"current_connections": 98, "max_connections": 100},
        )
    )

    assert result.category == "too_many_connections"
    assert result.severity == "high"
    assert result.checks_performed == []
    observed = " ".join(result.evidence_observed)
    assert "98" in observed
    assert "100" in observed
    assert result.runbook_references[0].filename == "too_many_connections.md"
    assert any("Do not terminate" in action for action in result.recommended_actions)


def test_missing_connection_counts_are_not_invented() -> None:
    result = TriageEngine().triage(
        TriageRequest(incident_description="Too many database connections")
    )

    assert not any("current_connections" in item for item in result.evidence_observed)
    assert any("not both available" in item for item in result.hypotheses)
    assert any("not both provided" in item for item in result.uncertainties)
