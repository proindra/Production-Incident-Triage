"""Sentence Transformers adapter for document and query embeddings."""

from __future__ import annotations

from pathlib import Path

import numpy as np
from sentence_transformers import SentenceTransformer


class SentenceTransformerEmbeddings:
    def __init__(self, model_name: str, cache_path: str | Path) -> None:
        self.model = SentenceTransformer(
            model_name,
            cache_folder=str(cache_path),
        )

    def embed(self, documents: list[str]) -> np.ndarray:
        return np.asarray(
            self.model.encode(
                documents,
                convert_to_numpy=True,
                show_progress_bar=False,
            ),
            dtype=np.float32,
        )

    def query_embed(self, query: str) -> np.ndarray:
        return self.embed([query])
