import sqlite3

from app.retrieval import RunbookRetriever
from app.schemas import TriageRequest
from app.storage.incidents import IncidentStore
from app.triage import TriageEngine


def test_runbook_retrieval_returns_existing_source_and_heading() -> None:
    references = RunbookRetriever().retrieve("disk usage wal replication slot")

    assert references
    assert references[0].filename == "disk_usage_wal_retention.md"
    assert references[0].heading
    assert references[0].excerpt


def test_runbook_retrieval_returns_no_citation_for_irrelevant_query() -> None:
    assert RunbookRetriever().retrieve("weekend weather forecast") == []


def test_incident_history_persists_metadata_without_raw_incident_text(tmp_path) -> None:
    sensitive_report = "Database latency report password=example-secret"
    response = TriageEngine().triage(TriageRequest(incident_description=sensitive_report))
    store = IncidentStore(tmp_path / "incidents.sqlite")

    incident_id = store.record(response)
    rows = store.list_recent()
    with sqlite3.connect(tmp_path / "incidents.sqlite") as connection:
        stored_text = str(connection.execute("SELECT * FROM incidents").fetchone())

    assert rows[0]["incident_id"] == incident_id
    assert rows[0]["category"] == "high_database_latency"
    assert rows[0]["status"] == "open"
    assert sensitive_report not in stored_text
    assert "example-secret" not in stored_text
