"""Read-only PostgreSQL adapter placeholder."""

from __future__ import annotations

from .base import BaseDiagnostic


class PostgresReadOnlyDiagnostic(BaseDiagnostic):
    """Reserved for safe, read-only Postgres checks."""

    def collect(self):
        """Collect read-only metrics safely."""
        return {}
