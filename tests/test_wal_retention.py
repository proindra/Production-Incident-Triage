from app.domain.schemas import TriageRequest
from app.services.retrieval import RunbookRetriever
from app.services.triage import TriageEngine
from langchain_core.messages import AIMessage, ToolMessage
import json


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


def test_model_selects_retrieval_tools_and_prompt_version_is_recorded(tmp_path) -> None:
    class ToolSelectingModel:
        def bind_tools(self, tools):
            self.tools = tools
            return self

        def invoke(self, messages):
            call_count = sum(isinstance(message, ToolMessage) for message in messages)
            calls = [
                (
                    "lookup_request_fields",
                    {
                        "request_json": json.dumps(
                            {
                                "incident_description": (
                                    "Disk usage increasing while table and index sizes are stable"
                                ),
                                "context": {},
                            }
                        )
                    },
                ),
                (
                    "search_corpus",
                    {"query": "replication slots retained WAL stable table sizes", "limit": 4},
                ),
                (
                    "lookup_clause_by_id",
                    {
                        "source_id": (
                            "disk_usage_wal_retention.md#replication slots"
                        )
                    },
                ),
                (
                    "lookup_clause_by_id",
                    {
                        "source_id": (
                            "postgresql/17/warm-standby.md#26.2.6. Replication Slots"
                        )
                    },
                ),
            ]
            if call_count >= len(calls):
                return AIMessage(content="Evidence retrieval is complete.")
            name, arguments = calls[call_count]
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": name,
                        "args": arguments,
                        "id": f"tool-{call_count}",
                        "type": "tool_call",
                    }
                ],
            )

    docs = tmp_path / "postgresql"
    (docs / "17").mkdir(parents=True)
    (docs / "17" / "warm-standby.md").write_text(
        "# PostgreSQL 17: Warm Standby\n\n"
        "Source: https://www.postgresql.org/docs/17/warm-standby.html\n\n"
        "## 26.2.6. Replication Slots\n\n"
        "Replication slots can cause PostgreSQL to retain WAL segments.\n",
        encoding="utf-8",
    )
    result = TriageEngine(
        tool_model=ToolSelectingModel(),
        retriever=RunbookRetriever(postgres_docs_path=docs),
    ).triage(
        TriageRequest(
            incident_description=(
                "Disk usage increasing while table and index sizes are stable"
            )
        )
    )

    assert {call["name"] for call in result.tool_calls} == {
        "lookup_request_fields",
        "search_corpus",
        "lookup_clause_by_id",
    }
    assert any(
        reference.source_version == "17"
        for reference in result.runbook_references
    )
    assert "triage-tool-agent-v1" in result.prompt_versions
    assert result.verification_passed is True


def test_postgres_document_retrieval_preserves_version_and_source_url(tmp_path) -> None:
    runbooks = tmp_path / "runbooks"
    docs = tmp_path / "postgresql"
    (docs / "17").mkdir(parents=True)
    runbooks.mkdir()
    (runbooks / "disk_usage_wal_retention.md").write_text(
        "# Disk\n\nCheck for WAL retention.", encoding="utf-8"
    )
    source_url = "https://www.postgresql.org/docs/17/warm-standby.html"
    (docs / "17" / "warm-standby.md").write_text(
        "# PostgreSQL 17: Warm Standby\n\n"
        f"Source: {source_url}\n\n"
        "## Replication Slots\n\n"
        "A replication slot retains WAL until its consumer advances.\n",
        encoding="utf-8",
    )

    retriever = RunbookRetriever(runbooks, postgres_docs_path=docs)
    references = retriever.retrieve("replication slots retain WAL consumer", limit=2)
    postgres_reference = next(
        reference for reference in references if reference.filename.startswith("postgresql/")
    )

    assert postgres_reference.heading == "Replication Slots"
    assert postgres_reference.source_version == "17"
    assert postgres_reference.source_url == source_url
    assert retriever.verifies(postgres_reference)


def test_ambiguous_categories_escalate_instead_of_selecting_first_match() -> None:
    result = TriageEngine().triage(
        TriageRequest(
            incident_description=(
                "Disk usage is rising while database latency is increasing"
            )
        )
    )

    assert result.category == "unknown"
    assert result.route == "page-secondary"
    assert result.cause_class == "unknown"


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
