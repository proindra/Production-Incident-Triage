"""Local, source-preserving retrieval over Markdown runbooks."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from app.schemas import RunbookReference


_WORD = re.compile(r"[a-z0-9_]+")
_IGNORED = {
    "about", "after", "all", "also", "and", "are", "before", "been", "but",
    "can", "check", "could", "does", "during", "for", "from", "have", "into",
    "may", "more", "need", "not", "only", "other", "should", "that", "their",
    "the", "there", "these", "this", "through", "was", "what", "when", "where",
    "which", "will", "with", "would", "you",
}


@dataclass(frozen=True)
class RunbookChunk:
    filename: str
    heading: str | None
    text: str


class RunbookRetriever:
    def __init__(self, runbooks_path: str | Path = "corpus/runbooks", chunk_size: int = 1200):
        self.path = Path(runbooks_path).resolve()
        self.chunk_size = chunk_size
        self._chunks = self._load()

    def retrieve(self, query: str, limit: int = 3) -> list[RunbookReference]:
        if limit <= 0:
            return []
        terms = _terms(query)
        if not terms:
            return []
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
        return results

    def _load(self) -> list[RunbookChunk]:
        if not self.path.is_dir():
            raise FileNotFoundError(f"Runbook directory not found: {self.path}")
        chunks: list[RunbookChunk] = []
        for source in sorted(self.path.glob("*.md")):
            resolved = source.resolve()
            if resolved.parent != self.path:
                continue
            chunks.extend(_split_document(source.name, source.read_text(encoding="utf-8"), self.chunk_size))
        return chunks


def _split_document(filename: str, text: str, chunk_size: int) -> list[RunbookChunk]:
    chunks: list[RunbookChunk] = []
    heading: str | None = None
    current: list[str] = []

    def flush() -> None:
        body = "\n".join(current).strip()
        if body:
            chunks.append(RunbookChunk(filename, heading, body))

    for line in text.splitlines():
        if line.startswith("#"):
            flush()
            current.clear()
            heading = line.lstrip("#").strip() or None
            continue
        current.append(line)
        if len("\n".join(current)) >= chunk_size:
            flush()
            current.clear()
    flush()
    return chunks


def _terms(text: str) -> set[str]:
    return {word for word in _WORD.findall(text.casefold()) if len(word) > 2 and word not in _IGNORED}
