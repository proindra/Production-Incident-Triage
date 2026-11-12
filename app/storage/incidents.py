"""Minimal SQLite incident history with deliberately limited data retention."""

from __future__ import annotations

import json
import logging
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from app.safety import redact_secrets
from app.schemas import TriageResponse

logger = logging.getLogger(__name__)


class StorageError(RuntimeError):
    """Incident history could not be written or initialized."""


class IncidentStore:
    def __init__(self, database_path: str | Path = "incidents.db") -> None:
        self.database_path = str(database_path)
        if self.database_path == ":memory:":
            raise ValueError("Incident history requires a file-backed SQLite database.")
        Path(self.database_path).parent.mkdir(parents=True, exist_ok=True)
        try:
            with self._connection() as connection:
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS incidents (
                        incident_id TEXT PRIMARY KEY,
                        timestamp TEXT NOT NULL,
                        category TEXT NOT NULL,
                        severity TEXT NOT NULL,
                        evidence_summary TEXT NOT NULL,
                        runbook_references TEXT NOT NULL,
                        recommendations TEXT NOT NULL,
                        status TEXT NOT NULL
                    )
                    """
                )
        except sqlite3.Error as error:
            logger.error("Incident history initialization failed (%s).", type(error).__name__)
            raise StorageError("Incident history could not be initialized.") from None

    def record(self, result: TriageResponse) -> str:
        incident_id = str(uuid.uuid4())
        timestamp = datetime.now(timezone.utc).isoformat()
        evidence_summary = redact_secrets(
            f"Triage completed for category {result.category}; "
            f"{len(result.evidence_observed)} evidence item(s) were assessed."
        )
        references = json.dumps(
            [
                {"filename": item.filename, "heading": item.heading}
                for item in result.runbook_references
            ]
        )
        recommendations = json.dumps(
            [redact_secrets(item) for item in result.recommended_actions]
        )
        try:
            with self._connection() as connection:
                connection.execute(
                    """
                    INSERT INTO incidents (
                        incident_id, timestamp, category, severity, evidence_summary,
                        runbook_references, recommendations, status
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        incident_id,
                        timestamp,
                        result.category,
                        result.severity,
                        evidence_summary,
                        references,
                        recommendations,
                        "open",
                    ),
                )
        except sqlite3.Error as error:
            logger.error("Incident history write failed (%s).", type(error).__name__)
            raise StorageError("Incident history could not be saved.") from None
        return incident_id

    def list_recent(self, limit: int = 50) -> list[dict[str, str]]:
        if not 1 <= limit <= 500:
            raise ValueError("limit must be between 1 and 500")
        try:
            with self._connection() as connection:
                rows = connection.execute(
                    """
                    SELECT incident_id, timestamp, category, severity,
                           evidence_summary, runbook_references, recommendations, status
                    FROM incidents
                    ORDER BY timestamp DESC
                    LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
        except sqlite3.Error as error:
            logger.error("Incident history read failed (%s).", type(error).__name__)
            raise StorageError("Incident history could not be read.") from None
        keys = (
            "incident_id", "timestamp", "category", "severity", "evidence_summary",
            "runbook_references", "recommendations", "status",
        )
        return [dict(zip(keys, row)) for row in rows]

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=3)
        connection.execute("PRAGMA busy_timeout = 3000")
        return connection

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()
