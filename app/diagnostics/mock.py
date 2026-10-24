"""Mock diagnostic adapter placeholder."""

from __future__ import annotations

from .base import BaseDiagnostic


class MockDiagnostic(BaseDiagnostic):
    """Deterministic sample incident data for tests."""

    def collect(self):
        """Return mock diagnostic data."""
        return {}
