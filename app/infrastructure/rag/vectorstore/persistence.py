"""Read and write the on-disk FAISS index and its JSON metadata."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any

import faiss

from app.infrastructure.rag.document_ingestion.loader import IndexedChunk

INDEX_FILENAME = "runbooks.faiss"
METADATA_FILENAME = "runbooks.json"
STATS_FILENAME = "runbooks-stats.json"
FORMAT_VERSION = 1


def load_index(
    path: Path,
    embedding_model: str | None = None,
    allow_missing: bool = False,
) -> tuple[Any | None, dict[str, Any]]:
    index_file = path / INDEX_FILENAME
    metadata_file = path / METADATA_FILENAME
    if not index_file.is_file() or not metadata_file.is_file():
        if allow_missing:
            return None, {}
        raise FileNotFoundError(
            f"FAISS runbook index is missing at {path}; "
            "run `python -m app.infrastructure.rag.document_ingestion` before enabling RAG."
        )

    try:
        index = faiss.read_index(str(index_file))
        with metadata_file.open(encoding="utf-8") as file:
            metadata = json.load(file)
        _validate_metadata(metadata)
    except (RuntimeError, ValueError, json.JSONDecodeError):
        if allow_missing:
            return None, {}
        raise

    if embedding_model is not None and metadata.get("embedding_model") != embedding_model:
        raise ValueError(
            "FAISS index embedding model does not match RAG_EMBEDDING_MODEL; "
            "rebuild the index."
        )
    if (
        index.d != metadata.get("dimension")
        or index.ntotal != len(metadata["chunks"])
    ):
        if allow_missing:
            return None, {}
        raise ValueError(
            "FAISS index and metadata are inconsistent; rebuild the index."
        )
    return index, metadata


def save_index(
    path: Path, index: Any, metadata: dict[str, Any]
) -> None:
    path.mkdir(parents=True, exist_ok=True)
    index_file = path / INDEX_FILENAME
    metadata_file = path / METADATA_FILENAME
    with tempfile.NamedTemporaryFile(
        dir=path, suffix=".faiss.tmp", delete=False
    ) as index_temp:
        index_temp_path = Path(index_temp.name)
    metadata_temp_path: Path | None = None
    stats_temp_path: Path | None = None
    try:
        faiss.write_index(index, str(index_temp_path))
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path,
            suffix=".json.tmp",
            delete=False,
        ) as metadata_temp:
            json.dump(metadata, metadata_temp, ensure_ascii=False, sort_keys=True)
            metadata_temp_path = Path(metadata_temp.name)
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path,
            suffix=".stats.tmp",
            delete=False,
        ) as stats_temp:
            json.dump(
                {
                    key: metadata[key]
                    for key in (
                        "format_version",
                        "embedding_model",
                        "dimension",
                        "built_at",
                        "last_update_duration_seconds",
                        "last_update_chunks_per_second",
                        "source_versions",
                    )
                    if key in metadata
                }
                | {"chunk_count": len(metadata.get("chunks", {}))},
                stats_temp,
                ensure_ascii=False,
                sort_keys=True,
            )
            stats_temp_path = Path(stats_temp.name)
        os.replace(index_temp_path, index_file)
        os.replace(metadata_temp_path, metadata_file)
        os.replace(stats_temp_path, path / STATS_FILENAME)
    finally:
        index_temp_path.unlink(missing_ok=True)
        if metadata_temp_path is not None:
            metadata_temp_path.unlink(missing_ok=True)
        if stats_temp_path is not None:
            stats_temp_path.unlink(missing_ok=True)


def vector_id(chunk_id: str) -> int:
    return int.from_bytes(
        hashlib.sha256(chunk_id.encode("utf-8")).digest()[:8], "big"
    ) & ((1 << 63) - 1)


def chunk_record(chunk: IndexedChunk) -> dict[str, Any]:
    return {
        "filename": chunk.filename,
        "heading": chunk.heading,
        "text": chunk.text,
        "content_hash": chunk.content_hash,
        "source_url": chunk.source_url,
        "source_version": chunk.source_version,
    }


def _validate_metadata(metadata: Any) -> None:
    if (
        not isinstance(metadata, dict)
        or metadata.get("format_version") != FORMAT_VERSION
        or not isinstance(metadata.get("chunks"), dict)
    ):
        raise ValueError("Unsupported FAISS metadata format.")
    if any(
        not isinstance(chunk_id, str)
        or not isinstance(record, dict)
        or not isinstance(record.get("content_hash"), str)
        or not isinstance(record.get("filename"), str)
        or not isinstance(record.get("text"), str)
        or (
            record.get("heading") is not None
            and not isinstance(record.get("heading"), str)
        )
        for chunk_id, record in metadata["chunks"].items()
    ):
        raise ValueError("FAISS metadata contains an invalid chunk record.")
