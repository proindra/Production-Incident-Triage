from app.domain.schemas import TriageRequest
from app.services.retrieval import RunbookRetriever
from app.services.triage import TriageEngine


def test_disk_growth_with_stable_tables_treats_wal_as_a_hypothesis() -> None:
    result = TriageEngine().triage(
        TriageRequest(
            incident_description="Disk usage is increasing while table and index sizes are stable",
            context={
                "disk_used_percent": 88,
                "disk_growth": "increasing",
                "table_index_sizes": "stable",
                "replication_slots": [
                    {"active": False, "restart_lsn": "0/1234", "wal_status": "reserved"}
                ],
                "consumer_health": "stalled",
            },
        )
    )

    assert result.category == "disk_usage_wal_retention"
    assert result.severity == "medium"
    assert result.checks_performed == []
    assert result.route == "self-serve runbook"
    assert any("possible explanation" in item for item in result.hypotheses)
    assert any("not confirm" in item for item in result.hypotheses)
    assert result.runbook_references[0].filename == "disk_usage_wal_retention.md"
    assert result.runbook_references[0].heading
    assert any("free space" in item.lower() for item in result.checks_recommended)


def test_inactive_slot_alone_does_not_confirm_wal_retention() -> None:
    result = TriageEngine().triage(
        TriageRequest(
            incident_description="Disk usage is increasing; table sizes are stable",
            context={
                "disk_growth": "increasing",
                "table_index_sizes": "stable",
                "replication_slots": [{"active": False, "restart_lsn": "0/1234"}],
            },
        )
    )

    assert any("possible explanation" in item for item in result.hypotheses)
    assert any("does not show both" in item for item in result.uncertainties)
    assert result.route == "page-secondary"


def test_growing_table_sizes_are_not_described_as_missing_or_stable() -> None:
    result = TriageEngine().triage(
        TriageRequest(
            incident_description="Disk usage is increasing",
            context={"disk_growth": "increasing", "table_index_sizes": "growing"},
        )
    )

    assert any("Table/index sizes are reported to be growing" in item for item in result.hypotheses)
    assert any("stable-size condition" in item for item in result.uncertainties)


def test_critical_disk_risk_escalates_without_waiting_for_cause() -> None:
    result = TriageEngine().triage(
        TriageRequest(
            incident_description="Disk usage is rising",
            context={"disk_used_percent": 97},
        )
    )

    assert result.severity == "critical"
    assert result.escalation_guidance.startswith("Escalate immediately")


def test_unsafe_remediation_is_refused() -> None:
    result = TriageEngine().triage(
        TriageRequest(
            incident_description="Disk is full; drop replication slot and delete WAL now"
        )
    )

    assert result.recommended_actions[0].startswith("Refused:")
    assert result.checks_performed == []
    assert any("Do not drop slots" in item for item in result.recommended_actions)


def test_langgraph_selects_retrieval_tools_and_verifies_citations() -> None:
    result = TriageEngine().triage(
        TriageRequest(
            incident_description=(
                "Disk usage is increasing while table and index sizes are stable"
            ),
            context={"disk_growth": "increasing", "table_index_sizes": "stable"},
        )
    )

    tools = {item["name"] for item in result.tool_calls}
    assert {"lookup_request_fields", "search_corpus", "lookup_clause_by_id"} <= tools
    assert result.agent_steps == 2
    assert result.verification_passed is True
    assert all(item["latency_ms"] >= 0 for item in result.tool_calls)


def test_unsupported_incident_does_not_search_runbooks() -> None:
    result = TriageEngine().triage(
        TriageRequest(incident_description="Weather forecast for the weekend")
    )

    tools = {item["name"] for item in result.tool_calls}
    assert tools == {"lookup_request_fields"}
    assert result.runbook_references == []


def test_unverified_category_is_escalated_without_recommendation(tmp_path) -> None:
    engine = TriageEngine(retriever=RunbookRetriever(tmp_path))

    result = engine.triage(
        TriageRequest(incident_description="High database latency is affecting users")
    )

    assert result.category == "unknown"
    assert result.verification_passed is False
    assert result.runbook_references == []
    assert result.route == "page-secondary"
    assert result.recommended_actions == [
        "Escalate to a human: no supporting runbook passage could be verified."
    ]
