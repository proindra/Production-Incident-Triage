"""Build or incrementally update the local FAISS runbook index."""

from __future__ import annotations

import json

from fastembed import TextEmbedding

from app.infrastructure.rag.config import RAGSettings
from app.infrastructure.rag.document_ingestion.loader import load_runbook_chunks
from app.infrastructure.rag.vectorstore import FAISSIndexBuilder


def main() -> None:
    settings = RAGSettings.from_environment()
    settings.require_embedding_settings()
    settings.embedding_cache_path.mkdir(parents=True, exist_ok=True)
    embeddings = TextEmbedding(
        model_name=settings.embedding_model,
        cache_dir=str(settings.embedding_cache_path),
    )
    index_builder = FAISSIndexBuilder(
        settings.index_path,
        settings.embedding_model,
        embeddings,
    )
    chunks = load_runbook_chunks(
        settings.runbooks_path,
        settings.chunk_size,
        (settings.postgres_docs_path,),
    )
    update = index_builder.update(chunks)
    print(json.dumps(update.__dict__, sort_keys=True))


if __name__ == "__main__":
    main()
