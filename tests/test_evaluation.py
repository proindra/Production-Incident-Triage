import pytest

from app.schemas import TriageRequest
from app.triage import TriageEngine


CASES = [
    ("latency-report", "Database latency is elevated", None, "high_database_latency", "medium", False),
    ("latency-three-times-threshold", "Slow database latency", {"observed_latency_ms": 1800, "threshold_latency_ms": 500}, "high_database_latency", "high", False),
    ("latency-no-checks", "High database latency", None, "high_database_latency", "medium", False),
    ("latency-user-impact", "High latency and customer requests are failing", None, "high_database_latency", "high", False),
    ("connections-normal-headroom", "Too many connections", {"current_connections": 50, "max_connections": 100}, "too_many_connections", "medium", False),
    ("connections-near-limit", "Connection pool is nearly exhausted", {"current_connections": 96, "max_connections": 100}, "too_many_connections", "high", False),
    ("connections-missing-counts", "Too many database connections", None, "too_many_connections", "medium", False),
    ("connections-unavailable", "Applications cannot connect because of connections", None, "too_many_connections", "high", False),
    ("disk-moderate-growth", "Disk usage increasing; table sizes stable", {"disk_used_percent": 88}, "disk_usage_wal_retention", "medium", False),
    ("disk-high-growth", "Disk usage is rising", {"disk_used_percent": 90}, "disk_usage_wal_retention", "high", False),
    ("disk-critical-growth", "Disk usage increasing", {"disk_used_percent": 95}, "disk_usage_wal_retention", "critical", False),
    ("disk-critical-free-space", "Disk volume is nearly full", {"disk_free_percent": 4}, "disk_usage_wal_retention", "critical", False),
    ("disk-missing-slot-evidence", "Disk is growing while table sizes are stable", None, "disk_usage_wal_retention", "medium", False),
    ("inactive-slot-not-proof", "Disk growth and an inactive replication slot", {"replication_slots": [{"active": False}]}, "disk_usage_wal_retention", "medium", False),
    ("conflicting-table-trend", "Disk usage increasing", {"disk_growth": "increasing", "table_index_sizes": "growing"}, "disk_usage_wal_retention", "medium", False),
    ("disk-other-consumers", "Disk usage is increasing; check logs and backups", None, "disk_usage_wal_retention", "medium", False),
    ("unknown-incident", "Something unusual happened", None, "unknown", "low", True),
    ("irrelevant-runbook", "Weather forecast for the weekend", None, "unknown", "low", True),
    ("refuse-wal-deletion", "Delete WAL files to make space", None, "disk_usage_wal_retention", "medium", False),
    ("refuse-session-termination", "Too many connections; terminate sessions", None, "too_many_connections", "medium", False),
]


@pytest.mark.parametrize(
    ("case_id", "description", "context", "category", "severity", "no_runbook"),
    CASES,
    ids=[case[0] for case in CASES],
)
def test_evaluation_case(
    case_id: str,
    description: str,
    context: dict | None,
    category: str,
    severity: str,
    no_runbook: bool,
) -> None:
    result = TriageEngine().triage(
        TriageRequest(incident_description=description, context=context)
    )

    assert result.category == category, case_id
    assert result.severity == severity, case_id
    assert result.checks_performed == [], case_id
    if no_runbook:
        assert result.runbook_references == []
    else:
        assert result.runbook_references
    if case_id.startswith("refuse-"):
        assert result.recommended_actions[0].startswith("Refused:")
