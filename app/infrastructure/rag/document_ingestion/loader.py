"""Source-preserving Markdown runbook chunking for vector indexing."""

from __future__ import annotations

import hashlib
import re
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
    source_url: str | None = None
    source_version: str | None = None

    @property
    def searchable_text(self) -> str:
        return f"{self.heading or ''}\n{self.text}".strip()


def load_runbook_chunks(
    runbooks_path: str | Path,
    chunk_size: int = 1200,
    additional_paths: tuple[str | Path, ...] = (),
) -> list[IndexedChunk]:
    chunks: list[IndexedChunk] = []
    roots = [(Path(runbooks_path).resolve(), True)]
    roots.extend((Path(path).resolve(), False) for path in additional_paths)
    if not roots[0][0].is_dir():
        raise FileNotFoundError(f"Runbook directory not found: {roots[0][0]}")

    for root, is_runbooks in roots:
        if not root.is_dir():
            continue
        for source in sorted(root.rglob("*.md")):
            resolved = source.resolve()
            if root not in resolved.parents:
                continue
            relative = source.relative_to(root).as_posix()
            filename = (
                source.name
                if is_runbooks and source.parent == root
                else f"postgresql/{relative}"
            )
            text = source.read_text(encoding="utf-8")
            source_url = _source_url(text)
            source_version = _source_version(filename)
            for index, chunk in enumerate(
                split_runbook_document(filename, text, chunk_size)
            ):
                content_hash = hashlib.sha256(chunk.text.encode("utf-8")).hexdigest()
                chunks.append(
                    IndexedChunk(
                        chunk_id=f"{filename}:{chunk.heading or ''}:{index}",
                        filename=filename,
                        heading=chunk.heading,
                        text=chunk.text,
                        content_hash=content_hash,
                        source_url=source_url,
                        source_version=source_version,
                    )
                )
    if not chunks:
        raise ValueError("No Markdown runbook or PostgreSQL documentation chunks found.")
    return chunks


def _source_url(text: str) -> str | None:
    for line in text.splitlines()[:8]:
        if line.startswith("Source: https://www.postgresql.org/docs/"):
            return line.removeprefix("Source: ").strip()
    return None


def _source_version(filename: str) -> str | None:
    match = re.match(r"postgresql/(\d+)/", filename)
    return match.group(1) if match else None
