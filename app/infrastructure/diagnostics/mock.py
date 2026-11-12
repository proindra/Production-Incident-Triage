"""Deterministic diagnostic adapter; it never invents measurements."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from app.domain.diagnostics import DiagnosticSnapshot


class MockDiagnostic:
    """Return only explicitly configured fixtures, for demos and tests."""

    def __init__(self, fixtures: dict[str, dict[str, Any]] | None = None) -> None:
        self._fixtures = fixtures or {}

    def collect(self, category: str) -> DiagnosticSnapshot:
        fixture = deepcopy(self._fixtures.get(category, {}))
        checks = fixture.pop("_checks_performed", [])
        unavailable = fixture.pop("_unavailable", [])
        return DiagnosticSnapshot(
            evidence=fixture,
            checks_performed=list(checks),
            unavailable=list(unavailable),
        )
