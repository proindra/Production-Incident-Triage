"""Contracts and result types shared by RAG services and adapters."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RAGGeneration:
    summary: str
    prompt_version: str
    prompt_tokens: int | None
    completion_tokens: int | None
    cost_usd: float | None = None


class RAGGenerationError(RuntimeError):
    """A generated summary could not be safely used."""
