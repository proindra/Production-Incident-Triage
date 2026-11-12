"""Retriever and Groq summary service used by the LangGraph workflow."""

from __future__ import annotations

from typing import Any

from fastembed import TextEmbedding
from langchain_groq import ChatGroq

from app.domain.rag import RAGGeneration
from app.domain.schemas import RunbookReference
from app.infrastructure.rag.config import RAGSettings
from app.infrastructure.rag.node.generate_summary import GroqSummaryGenerator
from app.infrastructure.rag.vectorstore import FAISSRunbookStore
from app.infrastructure.llm.resilience import ProviderResilience


class RunbookRAG:
    def __init__(
        self,
        store: FAISSRunbookStore,
        generator: GroqSummaryGenerator,
        top_k: int = 4,
    ) -> None:
        self.store = store
        self.generator = generator
        self.top_k = top_k

    @classmethod
    def from_settings(
        cls,
        settings: RAGSettings,
        provider_resilience: ProviderResilience | None = None,
    ) -> RunbookRAG:
        settings.require_generation_settings()
        embedding_model = settings.embedding_model
        api_key = settings.groq_api_key
        groq_model = settings.groq_model
        if not embedding_model or not api_key or not groq_model:
            raise ValueError("RAG generation settings are incomplete.")
        settings.embedding_cache_path.mkdir(parents=True, exist_ok=True)
        embeddings = TextEmbedding(
            model_name=embedding_model,
            cache_dir=str(settings.embedding_cache_path),
        )
        store = FAISSRunbookStore(
            settings.index_path,
            embedding_model,
            embeddings,
        )
        model = ChatGroq(
            api_key=api_key,
            model=groq_model,
            base_url=settings.groq_base_url,
            temperature=0,
            max_tokens=300,
            timeout=settings.timeout_seconds,
            max_retries=0,
        )
        return cls(
            store=store,
            generator=GroqSummaryGenerator(
                model,
                settings.input_cost_per_million,
                settings.output_cost_per_million,
                provider_resilience,
            ),
            top_k=settings.top_k,
        )

    def retrieve(self, query: str, limit: int | None = None) -> list[RunbookReference]:
        return self.store.retrieve(query, self.top_k if limit is None else limit)

    def generate_summary(
        self,
        incident_description: str,
        context: dict[str, Any],
        references: list[RunbookReference],
    ) -> RAGGeneration:
        return self.generator.generate(incident_description, context, references)
