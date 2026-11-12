"""Source-preserving Markdown runbook chunking for vector indexing."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from app.domain.runbooks import split_runbook_document


@dataclass(frozen=True)
class IndexedChunk:
    chunk_id: str
    filename: str
    heading: str | None
    text: str
    content_hash: str

    @property
    def searchable_text(self) -> str:
        return f"{self.heading or ''}\n{self.text}".strip()


def load_runbook_chunks(
    runbooks_path: str | Path, chunk_size: int = 1200
) -> list[IndexedChunk]:
    root = Path(runbooks_path).resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"Runbook directory not found: {root}")

    chunks: list[IndexedChunk] = []
    for source in sorted(root.glob("*.md")):
        resolved = source.resolve()
        if resolved.parent != root:
            continue
        for index, chunk in enumerate(
            split_runbook_document(
                source.name,
                source.read_text(encoding="utf-8"),
                chunk_size,
            )
        ):
            content_hash = hashlib.sha256(chunk.text.encode("utf-8")).hexdigest()
            chunks.append(
                IndexedChunk(
                    chunk_id=f"{source.name}:{chunk.heading or ''}:{index}",
                    filename=source.name,
                    heading=chunk.heading,
                    text=chunk.text,
                    content_hash=content_hash,
                )
            )
    if not chunks:
        raise ValueError(f"No Markdown runbook chunks found in {root}.")
    return chunks
