"""Ports for optional services consumed by triage orchestration."""

from __future__ import annotations

from typing import Any, Protocol

from app.domain.rag import RAGGeneration
from app.domain.schemas import RunbookReference


class RAGService(Protocol):
    def retrieve(
        self, query: str, limit: int | None = None
    ) -> list[RunbookReference]: ...

    def generate_summary(
        self,
        incident_description: str,
        context: dict[str, Any],
        references: list[RunbookReference],
    ) -> RAGGeneration: ...
