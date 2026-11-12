import pytest

from app.domain.policies import SafetyPolicy, Severity


@pytest.mark.parametrize(
    ("category", "description", "evidence", "expected"),
    [
        ("unknown", "Unclear report", {}, Severity.LOW),
        ("disk_usage_wal_retention", "Disk growing", {"disk_used_percent": 89}, Severity.MEDIUM),
        ("disk_usage_wal_retention", "Disk growing", {"disk_used_percent": 90}, Severity.HIGH),
        ("disk_usage_wal_retention", "Disk growing", {"disk_used_percent": 95}, Severity.CRITICAL),
        ("disk_usage_wal_retention", "Disk growing", {"disk_free_percent": 5}, Severity.CRITICAL),
        ("disk_usage_wal_retention", "Disk growing", {"disk_free_percent": 10}, Severity.HIGH),
        ("too_many_connections", "Connection count high", {"current_connections": 94, "max_connections": 100}, Severity.MEDIUM),
        ("too_many_connections", "Connection count high", {"current_connections": 95, "max_connections": 100}, Severity.HIGH),
        ("high_database_latency", "Latency elevated", {"observed_latency_ms": 1499, "threshold_latency_ms": 500}, Severity.MEDIUM),
        ("high_database_latency", "Latency elevated", {"observed_latency_ms": 1500, "threshold_latency_ms": 500}, Severity.HIGH),
        ("high_database_latency", "Database unavailable", {}, Severity.CRITICAL),
        ("too_many_connections", "Requests are failing", {}, Severity.HIGH),
    ],
)
def test_severity_thresholds(category, description, evidence, expected) -> None:
    assert SafetyPolicy().severity(category, description, evidence) == expected


@pytest.mark.parametrize(
    "description",
    [
        "Drop replication slot now",
        "Delete WAL files",
        "Terminate session 123",
        "Change max_connections",
        "Restart PostgreSQL",
    ],
)
def test_destructive_requests_are_flagged(description: str) -> None:
    assert SafetyPolicy.unsafe_request(description)


def test_safe_request_is_not_flagged() -> None:
    assert not SafetyPolicy.unsafe_request("Inspect replication slots read-only")
