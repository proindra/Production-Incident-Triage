"""Incrementally build and persist the FAISS index for runbook chunks."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import faiss
import numpy as np
from filelock import FileLock

from app.infrastructure.rag.document_ingestion.loader import IndexedChunk
from app.infrastructure.rag.vectorstore.persistence import (
    chunk_record,
    load_index,
    save_index,
    vector_id,
)


class EmbeddingModel(Protocol):
    def embed(self, documents: list[str]) -> Any: ...

    def query_embed(self, query: str) -> Any: ...


@dataclass(frozen=True)
class IndexUpdate:
    added: int
    changed: int
    removed: int
    unchanged: int
    total: int


class FAISSIndexBuilder:
    def __init__(
        self,
        index_path: str | Path,
        embedding_model: str,
        embeddings: EmbeddingModel,
    ) -> None:
        self.path = Path(index_path)
        self.embedding_model = embedding_model
        self.embeddings = embeddings

    def update(self, chunks: list[IndexedChunk]) -> IndexUpdate:
        if not chunks:
            raise ValueError("Cannot build a FAISS index without runbook chunks.")
        self.path.mkdir(parents=True, exist_ok=True)
        lock_file = self.path / "index.lock"
        with FileLock(str(lock_file), timeout=120):
            previous_index, previous_metadata = load_index(
                self.path, allow_missing=True
            )
            current_by_id = {chunk.chunk_id: chunk for chunk in chunks}
            if len(current_by_id) != len(chunks):
                raise ValueError("Runbook chunks contain duplicate stable IDs.")
            vector_ids = [vector_id(chunk_id) for chunk_id in current_by_id]
            if len(set(vector_ids)) != len(vector_ids):
                raise ValueError("Runbook chunk IDs produced a FAISS ID collision.")

            model_changed = (
                previous_metadata.get("embedding_model") != self.embedding_model
            )
            previous_records = previous_metadata.get("chunks", {})
            added_ids = set(current_by_id) - set(previous_records)
            changed_ids = {
                chunk_id
                for chunk_id in set(current_by_id) & set(previous_records)
                if current_by_id[chunk_id].content_hash
                != previous_records[chunk_id]["content_hash"]
            }
            removed_ids = set(previous_records) - set(current_by_id)
            unchanged_ids = set(current_by_id) - added_ids - changed_ids

            if previous_index is None or model_changed:
                updated_index, stored = self._rebuild(chunks)
                added_count = len(chunks) if previous_index is None else 0
                changed_count = 0 if previous_index is None else len(chunks)
                removed_count = 0 if previous_index is None else len(removed_ids)
                unchanged_count = 0
            else:
                updated_index = previous_index
                changed_or_removed = changed_ids | removed_ids
                if changed_or_removed:
                    old_vector_ids = [
                        vector_id(chunk_id) for chunk_id in changed_or_removed
                    ]
                    updated_index.remove_ids(
                        np.asarray(old_vector_ids, dtype=np.int64)
                    )

                changed_chunks = [
                    current_by_id[chunk_id]
                    for chunk_id in sorted(added_ids | changed_ids)
                ]
                if changed_chunks:
                    vectors = self._embed_documents(changed_chunks)
                    updated_index.add_with_ids(
                        vectors,
                        np.asarray(
                            [vector_id(chunk.chunk_id) for chunk in changed_chunks],
                            dtype=np.int64,
                        ),
                    )

                stored = {
                    chunk_id: chunk_record(chunk)
                    for chunk_id, chunk in current_by_id.items()
                }
                added_count = len(added_ids)
                changed_count = len(changed_ids)
                removed_count = len(removed_ids)
                unchanged_count = len(unchanged_ids)

            metadata = {
                "format_version": 1,
                "embedding_model": self.embedding_model,
                "dimension": int(updated_index.d),
                "chunks": stored,
            }
            save_index(self.path, updated_index, metadata)

        return IndexUpdate(
            added=added_count,
            changed=changed_count,
            removed=removed_count,
            unchanged=unchanged_count,
            total=len(stored),
        )

    def _rebuild(
        self, chunks: list[IndexedChunk]
    ) -> tuple[Any, dict[str, dict[str, Any]]]:
        vectors = self._embed_documents(chunks)
        index = faiss.IndexIDMap2(faiss.IndexFlatIP(vectors.shape[1]))
        index.add_with_ids(
            vectors,
            np.asarray([vector_id(chunk.chunk_id) for chunk in chunks], dtype=np.int64),
        )
        stored = {chunk.chunk_id: chunk_record(chunk) for chunk in chunks}
        return index, stored

    def _embed_documents(self, chunks: list[IndexedChunk]) -> np.ndarray:
        vectors = np.asarray(
            list(
                self.embeddings.embed(
                    [chunk.searchable_text for chunk in chunks]
                )
            ),
            dtype=np.float32,
        )
        if (
            vectors.ndim != 2
            or vectors.shape[0] != len(chunks)
            or not vectors.shape[1]
        ):
            raise ValueError("Embedding model returned invalid document vectors.")
        faiss.normalize_L2(vectors)
        return vectors
