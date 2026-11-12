"""Source-preserving runbook chunk types and Markdown splitting."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RunbookChunk:
    filename: str
    heading: str | None
    text: str


def split_runbook_document(
    filename: str, text: str, chunk_size: int
) -> list[RunbookChunk]:
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
