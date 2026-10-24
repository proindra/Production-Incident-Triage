"""Base class for PostgreSQL diagnostic adapters."""

from __future__ import annotations


class BaseDiagnostic:
    """Common interface for all diagnostic adapters."""

    def collect(self):
        """Return diagnostics for an incident."""
        raise NotImplementedError
