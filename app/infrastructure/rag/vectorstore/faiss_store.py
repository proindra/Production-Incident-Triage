"""Load and query the persisted FAISS runbook index."""

from __future__ import annotations

from collections import OrderedDict
from pathlib import Path
from threading import Lock

import faiss
import numpy as np

from app.domain.schemas import RunbookReference
from app.infrastructure.rag.document_ingestion.loader import IndexedChunk
from app.infrastructure.rag.vectorstore.index_builder import (
    EmbeddingModel,
    FAISSIndexBuilder,
    IndexUpdate,
)
from app.infrastructure.rag.vectorstore.persistence import load_index, vector_id


class FAISSRunbookStore:
    def __init__(
        self,
        index_path: str | Path,
        embedding_model: str,
        embeddings: EmbeddingModel,
        cache_size: int = 256,
        allow_rebuild: bool = False,
    ) -> None:
        self.path = Path(index_path)
        self.embedding_model = embedding_model
        self.embeddings = embeddings
        self.cache_size = cache_size
        self._query_cache: OrderedDict[
            tuple[str, int], list[RunbookReference]
        ] = OrderedDict()
        self._cache_lock = Lock()
        self._index, self._chunks = load_index(
            self.path,
            embedding_model=None if allow_rebuild else self.embedding_model,
            allow_missing=allow_rebuild,
        )
        self._index_builder = FAISSIndexBuilder(
            self.path, self.embedding_model, self.embeddings
        )

    def update(self, chunks: list[IndexedChunk]) -> IndexUpdate:
        update = self._index_builder.update(chunks)
        self._index, self._chunks = load_index(
            self.path, embedding_model=self.embedding_model
        )
        with self._cache_lock:
            self._query_cache.clear()
        return update

    def retrieve(self, query: str, limit: int = 4) -> list[RunbookReference]:
        if limit <= 0 or not query.strip():
            return []
        key = (query, limit)
        with self._cache_lock:
            cached = self._query_cache.get(key)
            if cached is not None:
                self._query_cache.move_to_end(key)
                return [item.model_copy() for item in cached]

        if self._index is None or self._index.ntotal == 0:
            return []
        query_vector = np.asarray(
            list(self.embeddings.query_embed(query)), dtype=np.float32
        )
        if query_vector.ndim == 1:
            query_vector = query_vector.reshape(1, -1)
        if query_vector.shape[0] != 1 or query_vector.shape[1] != self._index.d:
            raise ValueError("Embedding model returned an invalid query vector.")
        faiss.normalize_L2(query_vector)
        search_limit = min(max(limit * 3, limit), self._index.ntotal)
        _, ids = self._index.search(query_vector, search_limit)
        references: list[RunbookReference] = []
        seen: set[tuple[str, str | None]] = set()
        records_by_vector = {
            vector_id(chunk_id): record
            for chunk_id, record in self._chunks["chunks"].items()
        }
        for indexed_id in ids[0]:
            if indexed_id < 0:
                continue
            record = records_by_vector.get(int(indexed_id))
            if record is None:
                continue
            identity = (record["filename"], record["heading"])
            if identity in seen:
                continue
            seen.add(identity)
            references.append(
                RunbookReference(
                    filename=record["filename"],
                    heading=record["heading"],
                    excerpt=record["text"][:600].strip(),
                )
            )
            if len(references) >= limit:
                break
        with self._cache_lock:
            self._query_cache[key] = [item.model_copy() for item in references]
            self._query_cache.move_to_end(key)
            while len(self._query_cache) > self.cache_size:
                self._query_cache.popitem(last=False)
        return references
