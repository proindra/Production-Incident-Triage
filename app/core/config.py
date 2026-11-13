"""Environment-backed settings for the triage service."""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    incident_db_path: str
    runbooks_path: str
    redis_url: str
    otel_endpoint: str | None
    rate_limit_per_minute: int
    triage_stub_delay_seconds: float

    def __post_init__(self) -> None:
        if self.rate_limit_per_minute < 1:
            raise ValueError("RATE_LIMIT_PER_MINUTE must be positive.")
        if not 0 <= self.triage_stub_delay_seconds <= 30:
            raise ValueError("TRIAGE_STUB_DELAY_SECONDS must be between 0 and 30.")

    @classmethod
    def from_environment(cls) -> Settings:
        return cls(
            incident_db_path=os.getenv("INCIDENT_DB_PATH", "data/incidents.db"),
            runbooks_path=os.getenv("RUNBOOKS_PATH", "corpus/runbooks"),
            redis_url=os.getenv("REDIS_URL", "redis://redis:6379/0"),
            otel_endpoint=os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT"),
            rate_limit_per_minute=int(os.getenv("RATE_LIMIT_PER_MINUTE", "30")),
            triage_stub_delay_seconds=float(os.getenv("TRIAGE_STUB_DELAY_SECONDS", "0")),
        )


DEFAULT_SETTINGS = Settings.from_environment()
