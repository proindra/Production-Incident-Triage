"""Evidence-first incident triage orchestration."""

from __future__ import annotations

import json
import logging
from typing import Any, Callable

from app.domain.diagnostics import BaseDiagnostic, DiagnosticError, DiagnosticSnapshot
from app.domain.policies import SafetyPolicy, Severity
from app.domain.safety import redact_secrets
from app.domain.schemas import CauseClass, TriageRequest, TriageResponse
from app.services.contracts import RAGService
from app.services.retrieval import RunbookRetriever

logger = logging.getLogger(__name__)

_CONTEXT_FIELDS = {
    "active_sessions", "connections_by_state", "consumer_health", "current_connections",
    "database_sizes", "disk_free_gb", "disk_free_percent", "disk_growth",
    "disk_usage_trend", "disk_used_percent", "duration_minutes", "free_disk_gb",
    "max_connections", "observed_latency_ms", "replication_activity",
    "replication_slots", "service_impact", "table_index_sizes",
    "table_sizes_stable", "threshold_latency_ms", "wal_growth",
}


class TriageEngine:
    def __init__(
        self,
        diagnostic: BaseDiagnostic | None = None,
        retriever: RunbookRetriever | None = None,
        policy: SafetyPolicy | None = None,
        rag_service: RAGService | None = None,
        tool_model: Any | None = None,
        model_name: str | None = None,
        input_cost_per_million: float | None = None,
        output_cost_per_million: float | None = None,
        provider_resilience: Any | None = None,
        agent_metric_recorder: Callable[[str, str, float], None] | None = None,
    ) -> None:
        self.diagnostic = diagnostic
        self.retriever = retriever or RunbookRetriever()
        self.policy = policy or SafetyPolicy()
        from app.services.agents import IncidentTriageGraph

        self.agent = IncidentTriageGraph(
            self,
            rag_service=rag_service,
            tool_model=tool_model,
            model_name=model_name,
            input_cost_per_million=input_cost_per_million,
            output_cost_per_million=output_cost_per_million,
            provider_resilience=provider_resilience,
            agent_metric_recorder=agent_metric_recorder,
        )

    def triage(self, request: TriageRequest) -> TriageResponse:
        return self.agent.invoke(request)

    def _triage_once(
        self,
        request: TriageRequest,
        references: list | None = None,
    ) -> TriageResponse:
        description = redact_secrets(request.incident_description)
        category = _classify(description)
        context = request.context or {}
        safe_context = {
            key: value for key, value in context.items() if key.casefold() in _CONTEXT_FIELDS
        }
        diagnostic = DiagnosticSnapshot()
        diagnostic_error: str | None = None
        if category != "unknown" and self.diagnostic is not None:
            try:
                diagnostic = self.diagnostic.collect(category)
            except DiagnosticError as error:
                diagnostic_error = str(error)
                logger.warning("Synthetic diagnostic fixtures unavailable for %s: %s", category, error)

        evidence_values = {**safe_context, **diagnostic.evidence}
        evidence = [f"Incident report: {description}"]
        evidence.extend(
            f"User-supplied context ({key}): {_safe_json(value)}"
            for key, value in safe_context.items()
        )
        evidence.extend(
            f"Diagnostic result ({key}): {_safe_json(value)}"
            for key, value in diagnostic.evidence.items()
        )
        if diagnostic_error:
            evidence.append("No diagnostic measurements were collected.")

        hypotheses, uncertainties = _assess(category, evidence_values, description)
        checks_recommended = _checks(category, evidence_values)
        if diagnostic_error:
            uncertainties.append(diagnostic_error)
        elif not diagnostic.checks_performed:
            uncertainties.append(
                "No live system is connected; measurements in the request context are synthetic or replayed and user-supplied."
            )
        uncertainties.extend(diagnostic.unavailable)
        if category == "unknown":
            uncertainties.append(
                "The incident does not clearly match one of the three supported runbooks."
            )

        severity = self.policy.severity(category, description, evidence_values)
        recommendations = _recommendations(category)
        if self.policy.unsafe_request(description):
            recommendations.insert(
                0,
                "Refused: no destructive remediation was executed. Any slot, WAL, session, or setting change requires an authorized human and an approved procedure.",
            )
        if references is None:
            references = self.retriever.retrieve(
                f"{description} "
                + " ".join(f"{key} {value}" for key, value in safe_context.items()),
                limit=3,
            )
        confidence = _confidence(category, evidence_values, diagnostic)
        route, routing_basis = _route(severity, category, evidence_values)
        return TriageResponse(
            category=category,
            cause_class=_cause_class(category, description),
            severity=severity.value,
            confidence=confidence,
            route=route,
            routing_basis=routing_basis,
            evidence_observed=evidence,
            hypotheses=hypotheses,
            checks_performed=diagnostic.checks_performed,
            checks_recommended=checks_recommended,
            runbook_references=references,
            recommended_actions=recommendations,
            escalation_guidance=_escalation(severity),
            uncertainties=uncertainties,
        )

    @staticmethod
    def _classify_for_agent(request: TriageRequest) -> str:
        return _classify(request.incident_description)


def _classify(description: str) -> str:
    text = description.casefold().replace("-", " ").replace("_", " ")
    matches = [
        (category, terms)
        for category, terms in (
            (
                "disk_usage_wal_retention",
                ("disk", "wal", "replication slot", "storage", "volume"),
            ),
            (
                "too_many_connections",
                ("connection", "connections", "too many clients", "pool exhausted"),
            ),
            (
                "high_database_latency",
                ("latency", "slow query", "slow database", "database is slow"),
            ),
        )
        if any(term in text for term in terms)
    ]
    return matches[0][0] if len(matches) == 1 else "unknown"


def _cause_class(category: str, description: str) -> CauseClass:
    text = description.casefold()
    if any(term in text for term in ("misconfig", "configuration error", "wrong setting")):
        return "configuration"
    if any(
        term in text
        for term in ("dependency unavailable", "upstream unavailable", "service unavailable")
    ):
        return "dependency_failure"
    return {
        "disk_usage_wal_retention": "data_volume",
        "too_many_connections": "saturation",
        "high_database_latency": "contention",
    }.get(category, "unknown")


def _assess(
    category: str, values: dict[str, Any], description: str
) -> tuple[list[str], list[str]]:
    hypotheses: list[str] = []
    uncertainties: list[str] = []
    if category == "disk_usage_wal_retention":
        growing = _is_growing(values.get("disk_growth", values.get("disk_usage_trend"))) or (
            "disk" in description.casefold()
            and any(term in description.casefold() for term in ("increas", "rising", "growing"))
        )
        table_sizes = values.get("table_index_sizes", values.get("table_sizes_stable"))
        stable = (
            _is_stable(table_sizes)
            or _is_stable(values.get("table_sizes_stable"))
            or ("stable" in description.casefold() and "table" in description.casefold())
        )
        table_growth = _is_growing(table_sizes)
        slots = values.get("replication_slots")
        slot_signal = _slot_retention_signal(slots, values.get("consumer_health"))
        if growing and stable:
            hypotheses.append(
                "WAL retention is a possible explanation because disk growth is reported while table/index sizes are stable; this does not confirm the cause."
            )
        elif slot_signal:
            hypotheses.append(
                "A replication slot may retain WAL, but the available slot fields alone do not establish that it caused disk growth."
            )
        else:
            hypotheses.append(
                "WAL retention is one possible explanation, but the available evidence does not establish it as the cause."
            )
        if not growing:
            uncertainties.append("A time-aligned disk or WAL growth trend was not provided.")
        if not stable:
            if table_growth:
                hypotheses.append(
                    "Table/index sizes are reported to be growing and may also contribute to disk use."
                )
                uncertainties.append(
                    "Table/index growth is reported, so the stable-size condition for this WAL-retention scenario is not met."
                )
            else:
                uncertainties.append("A time-aligned table/index size comparison was not provided.")
        if not slots:
            uncertainties.append("Replication-slot state and retained-WAL evidence are unavailable.")
        elif not slot_signal:
            uncertainties.append(
                "The supplied slot evidence does not show both a retention indicator and stalled consumer progress."
            )
        if not values.get("consumer_health") and not values.get("replication_activity"):
            uncertainties.append("Replication/decoding consumer health or progress was not provided.")
        if not values.get("disk_free_percent") and not values.get("disk_free_gb"):
            uncertainties.append("Free space and the rate of disk growth are unknown.")
        hypotheses.append(
            "Logs, backups, temporary files, or other files on the same volume could also explain growth and should be checked."
        )
    elif category == "too_many_connections":
        current = _number(values.get("current_connections"))
        maximum = _number(values.get("max_connections"))
        if current is not None and maximum is not None and maximum > 0:
            hypotheses.append(
                "Connection capacity may be contributing; the supplied count is compared with the configured maximum, but impact and reserved slots still need verification."
            )
        else:
            hypotheses.append("Connection exhaustion is possible, but current and maximum counts are not both available.")
            uncertainties.append("Current and maximum connection counts were not both provided by a diagnostic check or context.")
        if not values.get("connections_by_state"):
            uncertainties.append("Connection breakdown by application and state is unavailable.")
    elif category == "high_database_latency":
        hypotheses.append(
            "Latency may relate to query activity, waits, blocking, resource pressure, or application-side conditions; no single cause is confirmed."
        )
        if not values.get("active_sessions"):
            uncertainties.append("Active-session and wait-event evidence is unavailable.")
        if not values.get("observed_latency_ms") or not values.get("threshold_latency_ms"):
            uncertainties.append("Observed latency and its applicable threshold were not both supplied.")
    return hypotheses, uncertainties


def _checks(category: str, values: dict[str, Any]) -> list[str]:
    if category == "disk_usage_wal_retention":
        checks = [
            "Compare timestamp-aligned disk/free-space and WAL-directory trends with database/table/index sizes.",
            "Inspect replication slots read-only, including slot type, restart_lsn, wal_status, and safe_wal_size where available.",
            "Check replication or logical-decoding consumer health and progress, plus PostgreSQL logs.",
            "Check whether logs, backups, temporary files, or other volume consumers explain the growth.",
        ]
        if not values.get("disk_free_percent") and not values.get("disk_free_gb"):
            checks.insert(0, "Obtain current free space and disk growth rate from the host or managed-service metrics.")
        return checks
    if category == "too_many_connections":
        return [
            "Read current and maximum connection counts and confirm timestamp and account visibility.",
            "Group sessions by application and state; inspect long-lived idle-in-transaction sessions.",
            "Correlate with application pool metrics, traffic, retries, and recent deployments.",
        ]
    if category == "high_database_latency":
        return [
            "Inspect active sessions and wait events for the incident time window.",
            "Check blocking relationships and connection usage using read-only queries.",
            "Correlate application latency with CPU, memory, disk I/O, logs, and recent changes.",
        ]
    return ["Provide the affected service, time window, observed symptoms, and available read-only metrics."]


def _recommendations(category: str) -> list[str]:
    common = "Keep all remediation advisory; require an authorized human to approve any production change."
    by_category = {
        "disk_usage_wal_retention": [
            "Escalate with disk headroom, growth rate, table/index trend, slot state, and consumer progress.",
            "Do not drop slots, delete WAL, or change PostgreSQL settings automatically.",
            common,
        ],
        "too_many_connections": [
            "Share connection counts and application/state breakdown with the database and service owners.",
            "Do not terminate sessions or change connection settings automatically.",
            common,
        ],
        "high_database_latency": [
            "Share time-aligned activity, wait, blocking, and application/resource evidence with the service owner.",
            "Do not cancel queries, terminate sessions, or change configuration automatically.",
            common,
        ],
        "unknown": [
            "Collect the recommended read-only evidence and route the incident to the responsible service owner.",
            common,
        ],
    }
    return list(by_category[category])


def _escalation(severity: Severity) -> str:
    if severity == Severity.CRITICAL:
        return "Escalate immediately to the database/on-call owner; availability or storage may be at immediate risk."
    if severity == Severity.HIGH:
        return "Escalate promptly to the database/service owner and include the observed impact and diagnostic evidence."
    if severity == Severity.MEDIUM:
        return "Have the responsible owner review the evidence and trend; escalate if impact increases or evidence remains ambiguous."
    return "Continue safe evidence collection and escalate if customer impact, rapid deterioration, or a safety concern is identified."


def _route(
    severity: Severity, category: str, evidence: dict[str, Any]
) -> tuple[str, str]:
    if severity == Severity.CRITICAL:
        return "incident-commander", "Critical severity requires immediate incident command."
    if severity == Severity.HIGH:
        return "page-secondary", "High severity requires prompt human review."
    if category == "unknown":
        return "page-secondary", "An unsupported or ambiguous incident requires human review."
    if category == "disk_usage_wal_retention" and not _slot_retention_signal(
        evidence.get("replication_slots"), evidence.get("consumer_health")
    ):
        return (
            "page-secondary",
            "WAL retention is not corroborated by slot and consumer-progress evidence.",
        )
    if category == "too_many_connections" and (
        _number(evidence.get("current_connections")) is None
        or _number(evidence.get("max_connections")) is None
    ):
        return "page-secondary", "Connection capacity is not confirmed by both counts."
    if category == "high_database_latency" and (
        _number(evidence.get("observed_latency_ms")) is None
        or _number(evidence.get("threshold_latency_ms")) is None
    ):
        return "page-secondary", "Latency impact is not quantified against a threshold."
    return "self-serve runbook", "A supported runbook applies and the severity is not high or critical."


def _confidence(category: str, values: dict[str, Any], diagnostic: DiagnosticSnapshot) -> float:
    if category == "unknown":
        return 0.2
    if diagnostic.checks_performed:
        return 0.85
    if values:
        return 0.7
    return 0.5


def _is_growing(value: Any) -> bool:
    return isinstance(value, str) and any(word in value.casefold() for word in ("increasing", "rising", "growing"))


def _is_stable(value: Any) -> bool:
    return isinstance(value, str) and "stable" in value.casefold() or value is True


def _slot_retention_signal(value: Any, consumer_health: Any = None) -> bool:
    if not isinstance(value, list):
        return False
    stalled_consumer = any(
        term in str(consumer_health or "").casefold()
        for term in ("stalled", "not progressing", "unhealthy")
    )
    for slot in value:
        if not isinstance(slot, dict):
            continue
        inactive = slot.get("active") is False
        has_lsn = bool(slot.get("restart_lsn"))
        stalled = stalled_consumer or any(
            term in str(slot.get("consumer_health", "")).casefold()
            for term in ("stalled", "not progressing", "unhealthy")
        )
        if inactive and has_lsn and stalled:
            return True
    return False


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _safe_json(value: Any) -> str:
    try:
        serialized = json.dumps(value, sort_keys=True, default=str)
    except (TypeError, ValueError):
        serialized = str(value)
    return redact_secrets(serialized)
