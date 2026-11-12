"""Local, source-preserving retrieval over Markdown runbooks."""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

from app.domain.runbooks import RunbookChunk, split_runbook_document
from app.domain.schemas import RunbookReference


_WORD = re.compile(r"[a-z0-9_]+")
_IGNORED = {
    "about", "after", "all", "also", "and", "are", "before", "been", "but",
    "can", "check", "could", "does", "during", "for", "from", "have", "into",
    "may", "more", "need", "not", "only", "other", "should", "that", "their",
    "the", "there", "these", "this", "through", "was", "what", "when", "where",
    "which", "will", "with", "would", "you",
}


class RunbookRetriever:
    def __init__(self, runbooks_path: str | Path = "corpus/runbooks", chunk_size: int = 1200):
        self.path = Path(runbooks_path).resolve()
        self.chunk_size = chunk_size
        self._chunks = self._load()

    def retrieve(self, query: str, limit: int = 3) -> list[RunbookReference]:
        return [
            reference.model_copy()
            for reference in self._retrieve_cached(query, limit)
        ]

    @lru_cache(maxsize=512)
    def _retrieve_cached(self, query: str, limit: int) -> tuple[RunbookReference, ...]:
        if limit <= 0:
            return ()
        terms = _terms(query)
        if not terms:
            return ()
        ranked: list[tuple[int, RunbookChunk]] = []
        for chunk in self._chunks:
            searchable = _terms(f"{chunk.heading or ''} {chunk.text}")
            score = len(terms & searchable)
            if score:
                ranked.append((score, chunk))
        ranked.sort(key=lambda result: (-result[0], result[1].filename, result[1].heading or ""))
        results: list[RunbookReference] = []
        seen: set[tuple[str, str | None]] = set()
        for _, chunk in ranked:
            identity = (chunk.filename, chunk.heading)
            if identity in seen:
                continue
            seen.add(identity)
            results.append(
                RunbookReference(
                    filename=chunk.filename,
                    heading=chunk.heading,
                    excerpt=chunk.text[:600].strip(),
                )
            )
            if len(results) >= limit:
                break
        return tuple(results)

    def lookup_clause(self, source_id: str) -> list[RunbookReference]:
        filename, separator, heading_query = source_id.partition("#")
        if not separator or not filename or not heading_query:
            return []
        heading_query = heading_query.casefold()
        return [
            RunbookReference(
                filename=chunk.filename,
                heading=chunk.heading,
                excerpt=chunk.text[:600].strip(),
            )
            for chunk in self._chunks
            if chunk.filename == filename
            and chunk.heading is not None
            and heading_query in chunk.heading.casefold()
        ]

    def verifies(self, reference: RunbookReference) -> bool:
        return any(
            chunk.filename == reference.filename
            and chunk.heading == reference.heading
            and reference.excerpt.strip()
            and reference.excerpt.strip() in chunk.text
            for chunk in self._chunks
        )

    def _load(self) -> list[RunbookChunk]:
        if not self.path.is_dir():
            raise FileNotFoundError(f"Runbook directory not found: {self.path}")
        chunks: list[RunbookChunk] = []
        for source in sorted(self.path.glob("*.md")):
            resolved = source.resolve()
            if resolved.parent != self.path:
                continue
            chunks.extend(
                split_runbook_document(
                    source.name,
                    source.read_text(encoding="utf-8"),
                    self.chunk_size,
                )
            )
        return chunks


def _terms(text: str) -> set[str]:
    return {word for word in _WORD.findall(text.casefold()) if len(word) > 2 and word not in _IGNORED}
