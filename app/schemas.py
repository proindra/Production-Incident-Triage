from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


IncidentCategory = Literal[
    "high_database_latency",
    "too_many_connections",
    "disk_usage_wal_retention",
    "unknown",
]
SeverityLevel = Literal["low", "medium", "high", "critical"]


class HealthResponse(BaseModel):
    status: Literal["ok"]


class TriageRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    incident_description: str = Field(
        min_length=1,
        max_length=10_000,
        description="A concise description of the incident being investigated.",
    )
    context: dict[str, Any] | None = Field(
        default=None,
        max_length=100,
        description="Optional diagnostic observations or other incident context.",
    )

    @field_validator("context")
    @classmethod
    def context_size_is_bounded(cls, context: dict[str, Any] | None) -> dict[str, Any] | None:
        if context is not None and len(json.dumps(context, default=str)) > 10_000:
            raise ValueError("context must be at most 10,000 characters when serialized")
        return context


class RunbookReference(BaseModel):
    filename: str = Field(description="Runbook filename under corpus/runbooks.")
    heading: str | None = Field(
        default=None,
        description="Relevant heading from the runbook, when available.",
    )
    excerpt: str = Field(description="Text excerpt supporting the reference.")


class TriageResponse(BaseModel):
    incident_id: str | None = Field(
        default=None, description="Stored incident history identifier, when persistence succeeds."
    )
    category: IncidentCategory = Field(description="Incident category, or unknown if undetermined.")
    severity: SeverityLevel = Field(description="Severity assigned according to the centralized safety policy.")
    confidence: float = Field(ge=0, le=1)
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
    recommended_actions: list[str]
    escalation_guidance: str
    uncertainties: list[str] = Field(
        description="Missing evidence or uncertainty that affects the assessment."
    )
