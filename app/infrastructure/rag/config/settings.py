"""Runtime configuration for the optional RAG pipeline."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class RAGSettings:
    enabled: bool
    groq_api_key: str | None = field(repr=False)
    groq_model: str | None
    embedding_model: str | None
    runbooks_path: Path
    index_path: Path
    embedding_cache_path: Path
    top_k: int
    chunk_size: int
    timeout_seconds: float

    @classmethod
    def from_environment(cls) -> RAGSettings:
        return cls(
            enabled=_read_bool("RAG_ENABLED", default=False),
            groq_api_key=_read_optional("GROQ_API_KEY"),
            groq_model=_read_optional("GROQ_MODEL"),
            embedding_model=_read_optional("RAG_EMBEDDING_MODEL"),
            runbooks_path=Path(os.getenv("RUNBOOKS_PATH", "corpus/runbooks")),
            index_path=Path(os.getenv("FAISS_INDEX_PATH", "data/faiss")),
            embedding_cache_path=Path(
                os.getenv("FASTEMBED_CACHE_DIR", "data/fastembed")
            ),
            top_k=_read_int("RAG_TOP_K", default=4, minimum=1, maximum=10),
            chunk_size=_read_int(
                "RAG_CHUNK_SIZE", default=1200, minimum=200, maximum=5000
            ),
            timeout_seconds=_read_float(
                "GROQ_TIMEOUT_SECONDS", default=30.0, minimum=1.0, maximum=120.0
            ),
        )

    def require_embedding_settings(self) -> None:
        if not self.embedding_model:
            raise ValueError(
                "RAG indexing requires RAG_EMBEDDING_MODEL to be set."
            )

    def require_generation_settings(self) -> None:
        self.require_embedding_settings()
        missing = [
            name
            for name, value in (
                ("GROQ_API_KEY", self.groq_api_key),
                ("GROQ_MODEL", self.groq_model),
            )
            if not value
        ]
        if missing:
            raise ValueError(
                "RAG_ENABLED requires these environment variables: "
                + ", ".join(missing)
                + "."
            )


def _read_optional(name: str) -> str | None:
    return os.getenv(name, "").strip() or None


def _read_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    normalized = value.casefold()
    if normalized not in {"true", "false"}:
        raise ValueError(f"{name} must be 'true' or 'false'.")
    return normalized == "true"


def _read_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError as error:
        raise ValueError(f"{name} must be an integer.") from error
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}.")
    return value


def _read_float(
    name: str, default: float, minimum: float, maximum: float
) -> float:
    try:
        value = float(os.getenv(name, str(default)))
    except ValueError as error:
        raise ValueError(f"{name} must be a number.") from error
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}.")
    return value
