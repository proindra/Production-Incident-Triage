"""Centralized severity assignment and non-destructive response rules."""

from __future__ import annotations

from enum import StrEnum
from typing import Any


class Severity(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class SafetyPolicy:
    """Apply consistent severity thresholds and advisory-only safeguards."""

    def severity(
        self, category: str, description: str, evidence: dict[str, Any]
    ) -> Severity:
        normalized = description.casefold()
        disk_percent = _number(evidence.get("disk_used_percent"))
        free_percent = _number(evidence.get("disk_free_percent"))
        if category == "disk_usage_wal_retention":
            if disk_percent is not None and disk_percent >= 95:
                return Severity.CRITICAL
            if free_percent is not None and free_percent <= 5:
                return Severity.CRITICAL
            if disk_percent is not None and disk_percent >= 90:
                return Severity.HIGH
            if free_percent is not None and free_percent <= 10:
                return Severity.HIGH
        if any(
            phrase in normalized
            for phrase in ("database is unavailable", "database unavailable", "outage")
        ):
            return Severity.CRITICAL
        if any(
            phrase in normalized
            for phrase in ("requests are failing", "cannot connect", "can't connect")
        ):
            return Severity.HIGH
        if category == "too_many_connections":
            current = _number(evidence.get("current_connections"))
            maximum = _number(evidence.get("max_connections"))
            if current is not None and maximum is not None and maximum > 0:
                if current >= maximum * 0.95:
                    return Severity.HIGH
        if category == "high_database_latency":
            latency = _number(evidence.get("observed_latency_ms"))
            threshold = _number(evidence.get("threshold_latency_ms"))
            if latency is not None and threshold is not None and threshold > 0:
                if latency >= threshold * 3:
                    return Severity.HIGH
        if category == "unknown":
            return Severity.LOW
        return Severity.MEDIUM

    @staticmethod
    def unsafe_request(description: str) -> bool:
        text = description.casefold()
        unsafe_terms = (
            "drop replication slot",
            "drop slot",
            "drop the slot",
            "delete wal",
            "delete the wal",
            "delete pg_wal",
            "remove wal",
            "remove pg_wal",
            "terminate session",
            "terminate the session",
            "terminate backend",
            "kill session",
            "kill backend",
            "change max_connections",
            "change max_slot_wal_keep_size",
            "restart postgres",
            "restart postgresql",
        )
        return any(term in text for term in unsafe_terms)


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
