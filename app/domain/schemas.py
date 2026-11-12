from __future__ import annotations

import json
import math
from uuid import UUID
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


IncidentCategory = Literal[
    "high_database_latency",
    "too_many_connections",
    "disk_usage_wal_retention",
    "unknown",
]
CauseClass = Literal[
    "saturation",
    "contention",
    "configuration",
    "dependency_failure",
    "data_volume",
    "unknown",
]
SeverityLevel = Literal["low", "medium", "high", "critical"]
Route = Literal["self-serve runbook", "page-secondary", "incident-commander"]
RAGStatus = Literal["disabled", "generated", "skipped", "unavailable"]
AlertType = Literal[
    "high_database_latency",
    "too_many_connections",
    "disk_usage_increasing",
    "disk_usage_wal_retention",
]

_CONTEXT_FIELDS = {
    "active_sessions",
    "connections_by_state",
    "consumer_health",
    "current_connections",
    "database_sizes",
    "disk_free_gb",
    "disk_free_percent",
    "disk_growth",
    "disk_usage_trend",
    "disk_used_percent",
    "duration_minutes",
    "free_disk_gb",
    "max_connections",
    "observed_latency_ms",
    "replication_activity",
    "replication_slots",
    "service_impact",
    "table_index_sizes",
    "table_sizes_stable",
    "threshold_latency_ms",
    "wal_growth",
}
_NUMERIC_FIELDS = {
    "current_connections",
    "disk_free_gb",
    "disk_free_percent",
    "disk_used_percent",
    "duration_minutes",
    "free_disk_gb",
    "max_connections",
    "observed_latency_ms",
    "threshold_latency_ms",
}
_PERCENT_FIELDS = {"disk_free_percent", "disk_used_percent"}
_TREND_VALUES = {"increasing", "rising", "growing", "stable", "decreasing", "falling", "unknown"}


class HealthResponse(BaseModel):
    status: Literal["ok"]


class RunAccepted(BaseModel):
    run_id: str
    status: str
    status_url: str


class RunStatus(BaseModel):
    run_id: str
    status: str
    result: TriageResponse | None = None
    error: str | None = None


class FeedbackRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    satisfied: bool
    reason: str = Field(default="", max_length=1_000)

    @model_validator(mode="after")
    def require_reason_for_negative_feedback(self) -> FeedbackRequest:
        if not self.satisfied and not self.reason:
            raise ValueError("A reason is required when feedback is unsatisfactory.")
        return self


class FeedbackAccepted(BaseModel):
    feedback_id: UUID
    incident_id: UUID
    status: Literal["recorded"] = "recorded"


class FeedbackSummary(BaseModel):
    total: int
    satisfied: int
    unsatisfied: int
    satisfaction_rate: float = Field(ge=0, le=1)


class AlertPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    incident_type: AlertType
    database: str | None = Field(default=None, min_length=1, max_length=128)
    environment: Literal["production", "staging", "development", "demo"] = "demo"
    postgres_version: Literal["17"] = "17"
    metrics: dict[str, Any] = Field(default_factory=dict, max_length=100)

    @field_validator("metrics")
    @classmethod
    def validate_metrics(cls, metrics: dict[str, Any]) -> dict[str, Any]:
        return _validate_context(metrics)


class TriageRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    incident_description: str = Field(
        default="",
        max_length=10_000,
        description="A concise description of the incident being investigated.",
    )
    alert: AlertPayload | None = None
    on_call_note: str | None = Field(default=None, max_length=2_000)
    context: dict[str, Any] | None = Field(
        default=None,
        max_length=100,
        description="Optional diagnostic observations or other incident context.",
    )

    @field_validator("context")
    @classmethod
    def context_size_is_bounded(cls, context: dict[str, Any] | None) -> dict[str, Any] | None:
        if context is not None:
            _validate_context(context)
            if len(json.dumps(context, default=str)) > 10_000:
                raise ValueError("context must be at most 10,000 characters when serialized")
        return context

    @model_validator(mode="after")
    def validate_contract(self) -> TriageRequest:
        if self.alert is None and not self.incident_description.strip():
            raise ValueError("Provide an alert payload or a non-blank incident description.")
        if self.alert is not None:
            if not self.incident_description.strip() and not (self.on_call_note or "").strip():
                raise ValueError("An alert payload without a description requires an on-call note.")
            alert_summary = " ".join(
                item
                for item in (
                    self.alert.incident_type.replace("_", " "),
                    self.alert.database or "",
                    self.on_call_note or "",
                )
                if item
            )
            self.incident_description = " ".join(
                part for part in (self.incident_description.strip(), alert_summary) if part
            )[:10_000]
            self.context = {**self.alert.metrics, **(self.context or {})}
        return self


def _validate_context(context: dict[str, Any]) -> dict[str, Any]:
    unknown = set(context) - _CONTEXT_FIELDS
    if unknown:
        raise ValueError(f"Unsupported context field(s): {', '.join(sorted(unknown))}")
    for key in _NUMERIC_FIELDS & context.keys():
        value = context[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{key} must be a number.")
        if not math.isfinite(value) or value < 0:
            raise ValueError(f"{key} must be a finite, non-negative number.")
        if key in _PERCENT_FIELDS and value > 100:
            raise ValueError(f"{key} must not exceed 100.")
    for key in ("disk_growth", "disk_usage_trend", "table_index_sizes", "wal_growth"):
        value = context.get(key)
        if value is not None and (
            not isinstance(value, str) or value.casefold() not in _TREND_VALUES
        ):
            raise ValueError(f"{key} must be one of the supported trend values.")
    if "table_sizes_stable" in context and not isinstance(context["table_sizes_stable"], bool):
        raise ValueError("table_sizes_stable must be a boolean.")
    if "replication_slots" in context:
        slots = context["replication_slots"]
        if not isinstance(slots, list) or len(slots) > 100:
            raise ValueError("replication_slots must be a list with at most 100 entries.")
        if any(not isinstance(slot, dict) for slot in slots):
            raise ValueError("each replication_slots entry must be an object.")
    if "current_connections" in context and "max_connections" not in context:
        raise ValueError("current_connections requires max_connections.")
    if "max_connections" in context and context["max_connections"] <= 0:
        raise ValueError("max_connections must be greater than zero.")
    return context


class RunbookReference(BaseModel):
    filename: str = Field(description="Path of the source within the indexed corpus.")
    heading: str | None = Field(
        default=None,
        description="Relevant heading from the runbook, when available.",
    )
    excerpt: str = Field(description="Text excerpt supporting the reference.")
    source_url: str | None = None
    source_version: str | None = None


class ModelUsage(BaseModel):
    step: str
    prompt_version: str
    model: str | None = None
    prompt_tokens: int | None = Field(default=None, ge=0)
    completion_tokens: int | None = Field(default=None, ge=0)
    cost_usd: float | None = Field(default=None, ge=0)


class TriageResponse(BaseModel):
    incident_id: str | None = Field(
        default=None, description="Stored incident history identifier, when persistence succeeds."
    )
    category: IncidentCategory = Field(description="Incident category, or unknown if undetermined.")
    cause_class: CauseClass = "unknown"
    severity: SeverityLevel = Field(description="Severity assigned according to the centralized safety policy.")
    confidence: float = Field(ge=0, le=1)
    route: Route = "self-serve runbook"
    routing_basis: str = "Route selected from the project severity policy."
    verification_passed: bool = True
    agent_steps: int = Field(default=0, ge=0)
    agent_timings: list[dict[str, Any]] = Field(default_factory=list)
    retrieval_retries: int = Field(default=0, ge=0)
    tool_calls: list[dict[str, Any]] = Field(default_factory=list)
    prompt_versions: list[str] = Field(
        default_factory=lambda: ["deterministic-policy-v1"]
    )
    safety_flags: list[str] = Field(default_factory=list)
    model_usage: list[ModelUsage] = Field(default_factory=list)
    estimated_cost_usd: float | None = Field(default=0.0, ge=0)
    evidence_observed: list[str] = Field(
        description="Facts directly present in the input or returned by executed checks."
    )
    hypotheses: list[str] = Field(
        description="Potential explanations, clearly distinguished from observed facts."
    )
    checks_performed: list[str] = Field(
        description="Checks that actually ran; do not include recommendations here."
    )
    checks_recommended: list[str] = Field(
        description="Safe checks recommended but not yet performed."
    )
    runbook_references: list[RunbookReference]
    rag_summary: str | None = Field(
        default=None,
        max_length=2_000,
        description="Optional Groq summary grounded in the verified runbook passages.",
    )
    rag_status: RAGStatus = "disabled"
    rag_warning: str | None = Field(default=None, max_length=300)
    rag_prompt_version: str | None = None
    rag_prompt_tokens: int | None = Field(default=None, ge=0)
    rag_completion_tokens: int | None = Field(default=None, ge=0)
    recommended_actions: list[str]
    escalation_guidance: str
    uncertainties: list[str] = Field(
        description="Missing evidence or uncertainty that affects the assessment."
    )
