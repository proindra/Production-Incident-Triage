"""Shared types for diagnostic adapters."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass
class DiagnosticSnapshot:
    evidence: dict[str, Any] = field(default_factory=dict)
    checks_performed: list[str] = field(default_factory=list)
    unavailable: list[str] = field(default_factory=list)


class DiagnosticError(RuntimeError):
    """A diagnostic adapter could not connect or complete its collection."""


class BaseDiagnostic(Protocol):
    def collect(self, category: str) -> DiagnosticSnapshot:
        """Collect only the fixed, read-only checks for a known category."""
