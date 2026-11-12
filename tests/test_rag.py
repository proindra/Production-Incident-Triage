from __future__ import annotations

import numpy as np
import pytest
from langchain_core.messages import AIMessage

from app.domain.rag import RAGGeneration, RAGGenerationError
from app.domain.schemas import RunbookReference, TriageRequest
from app.infrastructure.rag.document_ingestion.loader import load_runbook_chunks
from app.infrastructure.rag.config import RAGSettings
from app.infrastructure.rag.graph_builder.service import RunbookRAG
from app.infrastructure.rag.node.generate_summary import (
    GroqSummaryGenerator,
)
from app.infrastructure.rag.vectorstore import FAISSRunbookStore
from app.services.retrieval import RunbookRetriever
from app.services.triage import TriageEngine


class FakeEmbeddings:
    def __init__(self) -> None:
        self.document_batches: list[list[str]] = []

    def embed(self, documents: list[str]) -> list[np.ndarray]:
        self.document_batches.append(documents)
        return [_vector(text) for text in documents]

    def query_embed(self, query: str) -> list[np.ndarray]:
        return [_vector(query)]


def _vector(text: str) -> np.ndarray:
    tokens = set(text.casefold().split())
    return np.asarray(
        [
            float(bool(tokens & {"disk", "volume", "storage", "wal", "retention"})),
            float(bool(tokens & {"connection", "pool", "sessions"})),
            float(bool(tokens & {"latency", "slow", "waits"})),
            1.0,
        ],
        dtype=np.float32,
    )


def test_faiss_index_updates_only_changed_runbook_chunks(tmp_path) -> None:
    corpus = tmp_path / "runbooks"
    corpus.mkdir()
    disk = corpus / "disk.md"
    connections = corpus / "connections.md"
    disk.write_text("# Disk\n\nCheck WAL retention and disk usage.", encoding="utf-8")
    connections.write_text(
        "# Connections\n\nCheck the connection pool and sessions.",
        encoding="utf-8",
    )
    chunks = load_runbook_chunks(corpus)
    embeddings = FakeEmbeddings()
    store = FAISSRunbookStore(
        tmp_path / "faiss",
        "test-embeddings",
        embeddings,
        allow_rebuild=True,
    )

    initial = store.update(chunks)
    unchanged = store.update(chunks)
    disk.write_text(
        "# Disk\n\nCheck WAL retention, disk usage, and free space.",
        encoding="utf-8",
    )
    changed = store.update(load_runbook_chunks(corpus))
    reloaded = FAISSRunbookStore(
        tmp_path / "faiss", "test-embeddings", embeddings
    )
    references = reloaded.retrieve("volume free space", limit=2)

    assert initial.added == 2
    assert unchanged.unchanged == 2
    assert len(embeddings.document_batches) == 2
    assert changed.changed == 1
    assert len(embeddings.document_batches[-1]) == 1
    assert references[0].filename == "disk.md"
    assert references[0].heading == "Disk"

    rebuild = FAISSRunbookStore(
        tmp_path / "faiss",
        "replacement-embeddings",
        embeddings,
        allow_rebuild=True,
    )
    rebuilt = rebuild.update(load_runbook_chunks(corpus))

    assert rebuilt.changed == 2
    assert rebuilt.unchanged == 0


class FakeChatModel:
    def __init__(self, content: str) -> None:
        self.content = content
        self.messages = None

    def invoke(self, messages) -> AIMessage:
        self.messages = messages
        return AIMessage(
            content=self.content,
            usage_metadata={
                "input_tokens": 23,
                "output_tokens": 11,
                "total_tokens": 34,
            },
        )


def test_groq_summary_requires_a_citation_from_retrieved_passages() -> None:
    reference = RunbookReference(
        filename="disk.md",
        heading="Storage",
        excerpt="Compare disk and WAL growth before inferring a cause.",
    )
    generator = GroqSummaryGenerator(
        FakeChatModel(
            "Disk growth may involve WAL retention; confirm with trend evidence "
            "[disk.md#Storage]."
        )
    )

    result = generator.generate("Disk is growing", {}, [reference])

    assert result.prompt_version == "grounded-summary-v2"
    assert result.prompt_tokens == 23
    assert result.completion_tokens == 11
    assert "[disk.md#Storage]" in result.summary

    invalid = GroqSummaryGenerator(
        FakeChatModel("Disk is full; run a cleanup command.")
    )
    with pytest.raises(RAGGenerationError, match="cite only retrieved passages"):
        invalid.generate("Disk is growing", {}, [reference])

    unsafe = GroqSummaryGenerator(
        FakeChatModel(
            "Drop replication slots to free space [disk.md#Storage]."
        )
    )
    with pytest.raises(RAGGenerationError, match="unsafe remediation"):
        unsafe.generate("Disk is growing", {}, [reference])

    unsupported_assertion = GroqSummaryGenerator(
        FakeChatModel("The cause is confirmed [disk.md#Storage].")
    )
    with pytest.raises(RAGGenerationError, match="unsupported cause assertion"):
        unsupported_assertion.generate("Disk is growing", {}, [reference])


def test_groq_prompt_redacts_secrets_before_provider_call() -> None:
    reference = RunbookReference(
        filename="disk.md",
        heading="Storage",
        excerpt="Compare disk and WAL growth before inferring a cause.",
    )
    model = FakeChatModel("Disk growth is reported [disk.md#Storage].")
    generator = GroqSummaryGenerator(model)

    generator.generate(
        "Disk password=actual-secret",
        {"service_impact": "api_key=sensitive-token"},
        [reference],
    )

    assert model.messages is not None
    sent_prompt = model.messages[1].content
    assert "actual-secret" not in sent_prompt
    assert "sensitive-token" not in sent_prompt


def test_rag_enabled_requires_runtime_groq_configuration(monkeypatch) -> None:
    monkeypatch.setenv("RAG_ENABLED", "true")
    monkeypatch.setenv("RAG_EMBEDDING_MODEL", "configured-at-runtime")
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.delenv("GROQ_MODEL", raising=False)

    settings = RAGSettings.from_environment()

    with pytest.raises(ValueError, match="GROQ_API_KEY, GROQ_MODEL"):
        settings.require_generation_settings()


class FakeRAGService:
    def __init__(self) -> None:
        self.retriever = RunbookRetriever()

    def retrieve(self, query: str, limit: int = 4) -> list[RunbookReference]:
        return self.retriever.retrieve(query, limit)

    def generate_summary(
        self,
        incident_description: str,
        context: dict,
        references: list[RunbookReference],
    ) -> RAGGeneration:
        reference = references[0]
        citation = f"[{reference.filename}#{reference.heading}]"
        return RAGGeneration(
            summary=f"The runbook advises checking the available evidence. {citation}",
            prompt_version="grounded-summary-v1",
            prompt_tokens=18,
            completion_tokens=9,
        )


def test_langgraph_returns_verified_rag_summary_without_replacing_policy() -> None:
    result = TriageEngine(rag_service=FakeRAGService()).triage(
        TriageRequest(
            incident_description=(
                "Disk usage increasing while table and index sizes are stable"
            ),
            context={"disk_growth": "increasing", "table_index_sizes": "stable"},
        )
    )

    assert result.verification_passed is True
    assert result.rag_status == "generated"
    assert result.rag_summary is not None
    assert result.rag_prompt_tokens == 18
    assert result.recommended_actions[0].startswith("Escalate with disk headroom")


def test_groq_failure_falls_back_to_cited_deterministic_triage() -> None:
    class UnavailableRAGService(FakeRAGService):
        def generate_summary(
            self,
            incident_description: str,
            context: dict,
            references: list[RunbookReference],
        ) -> RAGGeneration:
            raise RAGGenerationError("provider unavailable")

    result = TriageEngine(rag_service=UnavailableRAGService()).triage(
        TriageRequest(incident_description="High database latency is affecting users")
    )

    assert result.rag_status == "unavailable"
    assert result.rag_summary is None
    assert result.rag_warning
    assert result.verification_passed is True
    assert result.runbook_references
