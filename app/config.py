"""Environment-backed settings for the triage service."""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    database_url: str | None
    incident_db_path: str
    runbooks_path: str

    @classmethod
    def from_environment(cls) -> Settings:
        return cls(
            database_url=os.getenv("DATABASE_URL"),
            incident_db_path=os.getenv("INCIDENT_DB_PATH", "incidents.db"),
            runbooks_path=os.getenv("RUNBOOKS_PATH", "corpus/runbooks"),
        )


DEFAULT_SETTINGS = Settings.from_environment()
